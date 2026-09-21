import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from accounts.models import User, WechatOfficialAccountIdentity

from .models import ProviderOrder


WECHAT_OAUTH_STATE_SALT = "dazzy.payments.wechat-official-oauth"
WECHAT_OAUTH_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
WECHAT_OAUTH_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
PAYMENT_RETURN_TARGETS = {
    "provider_order": ("/pages/booking/payment", "orderNo"),
    "activity_publish": ("/pages/activities/publish-payment", "id"),
    "activity_participation": ("/pages/activities/participation-payment", "id"),
}


class WechatOAuthConfigurationError(APIException):
    status_code = 503
    default_detail = "微信服务号授权尚未完成配置，请稍后重试。"
    default_code = "wechat_oauth_not_configured"


class WechatOAuthGatewayError(APIException):
    status_code = 502
    default_detail = "微信服务号授权暂时不可用，请稍后重试。"
    default_code = "wechat_oauth_gateway_error"


@dataclass(frozen=True)
class WechatOAuthConfig:
    app_id: str
    app_secret: str
    callback_url: str
    h5_payment_url: str
    state_max_age_seconds: int
    timeout_seconds: int

    @classmethod
    def from_settings(cls):
        return cls(
            app_id=settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip(),
            app_secret=settings.WECHAT_OFFICIAL_ACCOUNT_APP_SECRET.strip(),
            callback_url=settings.WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL.strip(),
            h5_payment_url=settings.WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL.strip(),
            state_max_age_seconds=settings.WECHAT_OAUTH_STATE_MAX_AGE_SECONDS,
            timeout_seconds=settings.WECHAT_OAUTH_TIMEOUT_SECONDS,
        )

    def validate(self, *, require_secret: bool):
        required = (self.app_id, self.callback_url, self.h5_payment_url)
        if any(not value for value in required) or (require_secret and not self.app_secret):
            raise WechatOAuthConfigurationError()
        for value, allow_fragment in (
            (self.callback_url, False),
            (self.h5_payment_url, True),
        ):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.netloc:
                raise WechatOAuthConfigurationError("微信授权回调地址必须使用公网 HTTPS。")
            if not allow_fragment and (parsed.query or parsed.fragment):
                raise WechatOAuthConfigurationError("微信授权回调地址不能包含查询参数或片段。")
        if not 60 <= self.state_max_age_seconds <= 1800:
            raise WechatOAuthConfigurationError("微信授权状态有效期配置无效。")
        if not 1 <= self.timeout_seconds <= 30:
            raise WechatOAuthConfigurationError("微信授权请求超时配置无效。")


def get_official_account_openid(*, user_id: int, app_id: str) -> str:
    return (
        WechatOfficialAccountIdentity.objects.filter(user_id=user_id, app_id=app_id)
        .values_list("openid", flat=True)
        .first()
        or ""
    )


def build_payment_authorization(
    *, user_id: int, order_no: str, payment_kind: str = "provider_order"
) -> dict:
    config = WechatOAuthConfig.from_settings()
    config.validate(require_secret=False)
    if payment_kind not in PAYMENT_RETURN_TARGETS:
        raise ValidationError({"authorization": "微信授权支付类型无效。"})
    openid = get_official_account_openid(user_id=user_id, app_id=config.app_id)
    if openid:
        return {"authorized": True, "authorize_url": ""}
    state = signing.dumps(
        {
            "user_id": user_id,
            "order_no": order_no,
            "payment_kind": payment_kind,
        },
        key=settings.SECRET_KEY,
        salt=WECHAT_OAUTH_STATE_SALT,
        compress=True,
    )
    query = urlencode(
        {
            "appid": config.app_id,
            "redirect_uri": config.callback_url,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
        }
    )
    return {
        "authorized": False,
        "authorize_url": f"{WECHAT_OAUTH_AUTHORIZE_URL}?{query}#wechat_redirect",
    }


def _exchange_code(*, config: WechatOAuthConfig, code: str) -> tuple[str, str]:
    query = urlencode(
        {
            "appid": config.app_id,
            "secret": config.app_secret,
            "code": code,
            "grant_type": "authorization_code",
        }
    )
    request = Request(
        f"{WECHAT_OAUTH_TOKEN_URL}?{query}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
        raise WechatOAuthGatewayError() from exc
    if not isinstance(payload, dict) or payload.get("errcode"):
        raise WechatOAuthGatewayError()
    openid = str(payload.get("openid", "")).strip()
    unionid = str(payload.get("unionid", "")).strip()
    if not openid:
        raise WechatOAuthGatewayError("微信授权未返回有效用户标识。")
    return openid, unionid


def _h5_payment_return_url(
    *, base_url: str, order_no: str, payment_kind: str = "provider_order"
) -> str:
    parsed = urlsplit(base_url)
    try:
        return_path, identifier_name = PAYMENT_RETURN_TARGETS[payment_kind]
    except KeyError as exc:
        raise ValidationError({"authorization": "微信授权支付类型无效。"}) from exc
    if parsed.fragment:
        _fragment_path, separator, fragment_query = parsed.fragment.partition("?")
        params = dict(parse_qsl(fragment_query if separator else "", keep_blank_values=True))
        params.update({identifier_name: order_no, "wechatAuthorized": "1"})
        fragment = f"{return_path}?{urlencode(params)}"
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.update({identifier_name: order_no, "wechatAuthorized": "1"})
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(params), parsed.fragment)
    )


def _payment_authorization_business_exists(
    *, payment_kind: str, order_no: str, user_id: int
) -> bool:
    now = timezone.now()
    if payment_kind == "provider_order":
        return ProviderOrder.objects.filter(
            order_no=order_no,
            customer_id=user_id,
            status=ProviderOrder.Status.PENDING_PAYMENT,
            payment_expires_at__gt=now,
        ).exists()
    from activities.models import (
        ActivityParticipationPaymentOrder,
        ActivityPublishOrder,
    )

    if payment_kind == "activity_publish":
        return ActivityPublishOrder.objects.filter(
            activity_id=order_no,
            payer_id=user_id,
            status=ActivityPublishOrder.Status.PENDING_PAYMENT,
            expires_at__gt=now,
        ).exists()
    if payment_kind == "activity_participation":
        return ActivityParticipationPaymentOrder.objects.filter(
            participation__activity_id=order_no,
            payer_id=user_id,
            status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
            expires_at__gt=now,
        ).exists()
    return False


def complete_payment_authorization(*, code: str, state: str) -> str:
    config = WechatOAuthConfig.from_settings()
    config.validate(require_secret=True)
    if not code or not state:
        raise ValidationError({"authorization": "微信授权缺少 code 或 state。"})
    try:
        context = signing.loads(
            state,
            key=settings.SECRET_KEY,
            salt=WECHAT_OAUTH_STATE_SALT,
            max_age=config.state_max_age_seconds,
        )
    except signing.BadSignature as exc:
        raise ValidationError({"authorization": "微信授权状态无效或已过期。"}) from exc
    if not isinstance(context, dict):
        raise ValidationError({"authorization": "微信授权状态格式无效。"})
    user_id = context.get("user_id")
    order_no = str(context.get("order_no", ""))
    payment_kind = str(context.get("payment_kind", "provider_order"))
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not _payment_authorization_business_exists(
        payment_kind=payment_kind,
        order_no=order_no,
        user_id=user_id,
    ):
        raise ValidationError({"authorization": "待支付订单不存在或已失效。"})

    openid, unionid = _exchange_code(config=config, code=code)
    try:
        with transaction.atomic():
            WechatOfficialAccountIdentity.objects.update_or_create(
                user=user,
                app_id=config.app_id,
                defaults={
                    "openid": openid,
                    "unionid": unionid,
                    "authorized_at": timezone.now(),
                },
            )
    except IntegrityError as exc:
        raise ValidationError({"authorization": "该微信身份已绑定其他账号。"}) from exc
    return _h5_payment_return_url(
        base_url=config.h5_payment_url,
        order_no=order_no,
        payment_kind=payment_kind,
    )
