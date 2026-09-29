"""Wechat H5/App OAuth login, deliberately separate from payment authorization."""

import json
import secrets
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from .account_closure import can_attempt_interactive_login
from .models import User, WechatLoginIdentity, WechatUnionIdentity


WECHAT_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
WECHAT_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
STATE_PREFIX = "wechat:login:h5:state:"
TICKET_PREFIX = "wechat:login:ticket:"


class WechatLoginConfigurationError(APIException):
    status_code = 503
    default_detail = "微信登录尚未完成配置，请使用手机号登录。"
    default_code = "wechat_login_not_configured"


class WechatLoginGatewayError(APIException):
    status_code = 502
    default_detail = "微信登录服务暂时不可用，请稍后重试。"
    default_code = "wechat_login_gateway_error"


@dataclass(frozen=True)
class WechatIdentity:
    channel: str
    app_id: str
    openid: str
    unionid: str


def _ttl() -> int:
    value = settings.WECHAT_LOGIN_TICKET_TTL_SECONDS
    if not 60 <= value <= 900:
        raise WechatLoginConfigurationError()
    return value


def _credentials(channel: str) -> tuple[str, str]:
    if channel == WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT:
        app_id = settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip()
        secret = settings.WECHAT_OFFICIAL_ACCOUNT_APP_SECRET.strip()
    elif channel == WechatLoginIdentity.Channel.MOBILE_APP:
        app_id = settings.WECHAT_MOBILE_APP_ID.strip()
        secret = settings.WECHAT_MOBILE_APP_SECRET.strip()
    else:
        raise ValidationError({"channel": "不支持的微信登录渠道。"})
    if not app_id or not secret:
        raise WechatLoginConfigurationError()
    return app_id, secret


def _h5_urls() -> tuple[str, str]:
    callback = settings.WECHAT_H5_LOGIN_CALLBACK_URL.strip()
    return_url = settings.WECHAT_H5_LOGIN_RETURN_URL.strip()
    callback_parts = urlsplit(callback)
    return_parts = urlsplit(return_url)
    if (
        callback_parts.scheme != "https"
        or not callback_parts.netloc
        or callback_parts.query
        or callback_parts.fragment
        or return_parts.scheme != "https"
        or not return_parts.netloc
    ):
        raise WechatLoginConfigurationError()
    return callback, return_url


def begin_h5_login() -> tuple[str, str]:
    app_id, _secret = _credentials(WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT)
    callback, _return_url = _h5_urls()
    state = secrets.token_urlsafe(24)
    cache.set(f"{STATE_PREFIX}{state}", True, _ttl())
    query = urlencode(
        {
            "appid": app_id,
            "redirect_uri": callback,
            "response_type": "code",
            "scope": "snsapi_base",
            "state": state,
        }
    )
    return f"{WECHAT_AUTHORIZE_URL}?{query}#wechat_redirect", state


def _exchange_code(*, channel: str, code: str) -> WechatIdentity:
    app_id, secret = _credentials(channel)
    query = urlencode(
        {"appid": app_id, "secret": secret, "code": code, "grant_type": "authorization_code"}
    )
    request = Request(
        f"{WECHAT_TOKEN_URL}?{query}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=settings.WECHAT_OAUTH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
        raise WechatLoginGatewayError() from exc
    if not isinstance(payload, dict) or payload.get("errcode"):
        raise WechatLoginGatewayError()
    openid = str(payload.get("openid", "")).strip()
    if not openid:
        raise WechatLoginGatewayError("微信授权未返回用户标识。")
    return WechatIdentity(
        channel=channel,
        app_id=app_id,
        openid=openid,
        unionid=str(payload.get("unionid", "")).strip(),
    )


def _issue_ticket(identity: WechatIdentity) -> str:
    ticket = secrets.token_urlsafe(32)
    cache.set(f"{TICKET_PREFIX}{ticket}", identity.__dict__, _ttl())
    return ticket


def begin_mobile_login(code: str) -> str:
    return _issue_ticket(_exchange_code(channel=WechatLoginIdentity.Channel.MOBILE_APP, code=code))


def _h5_return_url(*, parameter: str, value: str) -> str:
    _callback, return_url = _h5_urls()
    parts = urlsplit(return_url)
    if parts.fragment:
        fragment_path, separator, fragment_query = parts.fragment.partition("?")
        query = dict(parse_qsl(fragment_query if separator else "", keep_blank_values=True))
        query[parameter] = value
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                parts.query,
                f"{fragment_path}?{urlencode(query)}",
            )
        )
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[parameter] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def complete_h5_callback(*, code: str, state: str) -> str:
    _app_id, _secret = _credentials(WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT)
    _h5_urls()
    if not state or not cache.get(f"{STATE_PREFIX}{state}"):
        raise ValidationError({"authorization": "微信授权已失效，请重新登录。"})
    if not cache.add(f"{STATE_PREFIX}{state}:used", True, _ttl()):
        raise ValidationError({"authorization": "微信授权已使用，请重新登录。"})
    cache.delete(f"{STATE_PREFIX}{state}")
    if not code:
        return _h5_return_url(parameter="wechatError", value="cancelled")
    ticket = _issue_ticket(
        _exchange_code(channel=WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT, code=code)
    )
    return _h5_return_url(parameter="wechatTicket", value=ticket) + f"&wechatState={state}"


def load_ticket(ticket: str) -> WechatIdentity:
    if not ticket or cache.get(f"{TICKET_PREFIX}{ticket}:used"):
        raise ValidationError({"ticket": "微信登录已失效，请重新授权。"})
    payload = cache.get(f"{TICKET_PREFIX}{ticket}")
    if not isinstance(payload, dict):
        raise ValidationError({"ticket": "微信登录已失效，请重新授权。"})
    try:
        identity = WechatIdentity(**payload)
    except TypeError as exc:
        raise ValidationError({"ticket": "微信登录凭证无效。"}) from exc
    if identity.channel not in WechatLoginIdentity.Channel.values:
        raise ValidationError({"ticket": "微信登录凭证无效。"})
    return identity


def _consume_ticket(ticket: str) -> None:
    if not cache.add(f"{TICKET_PREFIX}{ticket}:used", True, _ttl()):
        raise ValidationError({"ticket": "微信登录已使用，请重新授权。"})
    cache.delete(f"{TICKET_PREFIX}{ticket}")


def _ensure_active(user: User) -> None:
    if not can_attempt_interactive_login(user):
        raise ValidationError("账号当前不可用，请联系客服。")


@transaction.atomic
def _attach_identity(*, identity: WechatIdentity, user: User) -> None:
    if identity.unionid and settings.WECHAT_CROSS_CHANNEL_UNIONID_ENABLED:
        if WechatLoginIdentity.objects.filter(unionid=identity.unionid).exclude(user=user).exists():
            raise ValidationError("该微信已绑定其他账号，请联系客服处理。")
        # get_or_create relies on the unique UnionID constraint. Concurrent H5
        # and App bindings lock different users, so locking User alone is not enough.
        owner, _created = WechatUnionIdentity.objects.get_or_create(
            unionid=identity.unionid,
            defaults={"user": user},
        )
        if owner.user_id != user.pk:
            raise ValidationError("该微信已绑定其他账号，请联系客服处理。")
    existing = WechatLoginIdentity.objects.filter(
        app_id=identity.app_id, openid=identity.openid
    ).first()
    if existing and existing.user_id != user.pk:
        raise ValidationError("该微信已绑定其他账号，请联系客服处理。")
    other = WechatLoginIdentity.objects.filter(user=user, app_id=identity.app_id).first()
    if other and other.openid != identity.openid:
        raise ValidationError("该账号已绑定其他微信，请联系客服处理。")
    if existing:
        existing.unionid = identity.unionid or existing.unionid
        existing.authorized_at = timezone.now()
        existing.save(update_fields=("unionid", "authorized_at", "updated_at"))
        return
    try:
        WechatLoginIdentity.objects.create(
            user=user,
            channel=identity.channel,
            app_id=identity.app_id,
            openid=identity.openid,
            unionid=identity.unionid,
            authorized_at=timezone.now(),
        )
    except IntegrityError as exc:
        raise ValidationError("微信账号绑定冲突，请重新登录或联系客服。") from exc


@transaction.atomic
def resolve_login(ticket: str) -> User | None:
    identity = load_ticket(ticket)
    existing = (
        WechatLoginIdentity.objects.select_related("user")
        .filter(app_id=identity.app_id, openid=identity.openid)
        .first()
    )
    user = existing.user if existing else None
    if user is None and identity.unionid and settings.WECHAT_CROSS_CHANNEL_UNIONID_ENABLED:
        user_ids = list(
            WechatLoginIdentity.objects.filter(unionid=identity.unionid)
            .values_list("user_id", flat=True)
            .distinct()[:2]
        )
        if len(user_ids) > 1:
            raise ValidationError("微信身份关联了多个账号，请联系客服处理。")
        if user_ids:
            user = User.objects.select_for_update().get(pk=user_ids[0])
    if user is None:
        return None
    _ensure_active(user)
    _consume_ticket(ticket)
    _attach_identity(identity=identity, user=user)
    return user


@transaction.atomic
def bind_phone(*, ticket: str, phone: str, code: str, invite_code=None) -> tuple[User, bool]:
    from growth.services import register_invited_user

    from .services import verify_sms_code

    identity = load_ticket(ticket)
    # A verified phone is required even if an account with the same UnionID exists.
    verify_sms_code(phone=phone, purpose="wechat_bind", code=code)
    _consume_ticket(ticket)
    user = User.objects.select_for_update().filter(phone=phone).first()
    created = False
    if user is None:
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    phone=phone, password=None, nickname=f"用户{phone[-4:]}"
                )
                created = True
        except IntegrityError:
            user = User.objects.select_for_update().get(phone=phone)
    _ensure_active(user)
    _attach_identity(identity=identity, user=user)
    if created:
        register_invited_user(user=user, invite_code=invite_code)
    return user, created
