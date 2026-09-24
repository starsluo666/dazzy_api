import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import APIException


WECHAT_CODE_SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
WECHAT_ACCESS_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WECHAT_PHONE_NUMBER_URL = "https://api.weixin.qq.com/wxa/business/getuserphonenumber"


class WechatMiniProgramConfigurationError(APIException):
    status_code = 503
    default_detail = "微信小程序登录尚未完成配置，请稍后重试。"
    default_code = "wechat_mini_program_not_configured"


class WechatMiniProgramGatewayError(APIException):
    status_code = 502
    default_detail = "微信登录服务暂时不可用，请稍后重试。"
    default_code = "wechat_mini_program_gateway_error"


@dataclass(frozen=True)
class WechatMiniProgramConfig:
    app_id: str
    app_secret: str
    timeout_seconds: int

    @classmethod
    def from_client_type(cls, client_type: str):
        prefix = "CUSTOMER" if client_type == "customer" else "PROVIDER"
        config = cls(
            app_id=str(
                getattr(settings, f"WECHAT_{prefix}_MINI_PROGRAM_APP_ID", "")
            ).strip(),
            app_secret=str(
                getattr(settings, f"WECHAT_{prefix}_MINI_PROGRAM_APP_SECRET", "")
            ).strip(),
            timeout_seconds=settings.WECHAT_MINI_PROGRAM_TIMEOUT_SECONDS,
        )
        if not config.app_id or not config.app_secret:
            raise WechatMiniProgramConfigurationError()
        if not 1 <= config.timeout_seconds <= 30:
            raise WechatMiniProgramConfigurationError("微信登录请求超时配置无效。")
        return config


def _read_json(request: Request, *, timeout_seconds: int) -> dict:
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
        raise WechatMiniProgramGatewayError() from exc
    if not isinstance(payload, dict) or payload.get("errcode"):
        raise WechatMiniProgramGatewayError()
    return payload


def exchange_login_code(*, config: WechatMiniProgramConfig, code: str) -> tuple[str, str]:
    query = urlencode(
        {
            "appid": config.app_id,
            "secret": config.app_secret,
            "js_code": code,
            "grant_type": "authorization_code",
        }
    )
    payload = _read_json(
        Request(
            f"{WECHAT_CODE_SESSION_URL}?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        ),
        timeout_seconds=config.timeout_seconds,
    )
    openid = str(payload.get("openid", "")).strip()
    unionid = str(payload.get("unionid", "")).strip()
    if not openid:
        raise WechatMiniProgramGatewayError("微信登录未返回有效用户标识。")
    return openid, unionid


def _access_token(*, config: WechatMiniProgramConfig) -> str:
    cache_key = f"wechat:mini-program:access-token:{config.app_id}"
    cached = str(cache.get(cache_key) or "").strip()
    if cached:
        return cached
    query = urlencode(
        {
            "grant_type": "client_credential",
            "appid": config.app_id,
            "secret": config.app_secret,
        }
    )
    payload = _read_json(
        Request(
            f"{WECHAT_ACCESS_TOKEN_URL}?{query}",
            headers={"Accept": "application/json"},
            method="GET",
        ),
        timeout_seconds=config.timeout_seconds,
    )
    token = str(payload.get("access_token", "")).strip()
    if not token:
        raise WechatMiniProgramGatewayError("微信登录未返回有效访问凭证。")
    expires_in = max(60, int(payload.get("expires_in", 7200)) - 300)
    cache.set(cache_key, token, expires_in)
    return token


def exchange_phone_code(*, config: WechatMiniProgramConfig, code: str) -> str:
    token = _access_token(config=config)
    body = json.dumps({"code": code}).encode("utf-8")
    payload = _read_json(
        Request(
            f"{WECHAT_PHONE_NUMBER_URL}?{urlencode({'access_token': token})}",
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        ),
        timeout_seconds=config.timeout_seconds,
    )
    phone_info = payload.get("phone_info")
    phone = str(phone_info.get("purePhoneNumber", "")).strip() if isinstance(phone_info, dict) else ""
    if not phone:
        raise WechatMiniProgramGatewayError("微信授权未返回有效手机号。")
    return phone
