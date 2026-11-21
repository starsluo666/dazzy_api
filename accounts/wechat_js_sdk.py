import hashlib
import json
import secrets
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import APIException, ValidationError


class WechatNavigationUnavailable(APIException):
    status_code = 503
    default_detail = "微信地图暂时不可用。"


def _read_json(request):
    try:
        with urlopen(request, timeout=settings.WECHAT_OAUTH_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (URLError, TimeoutError, ValueError) as exc:
        raise WechatNavigationUnavailable() from exc
    if not isinstance(payload, dict) or payload.get("errcode"):
        raise WechatNavigationUnavailable()
    return payload


def _cached_credential(key, field, request):
    value = cache.get(key)
    if value:
        return value
    payload = _read_json(request)
    value = payload.get(field)
    expires_in = payload.get("expires_in")
    if not isinstance(value, str) or not value or not isinstance(expires_in, int) or expires_in <= 0:
        raise WechatNavigationUnavailable()
    cache.set(key, value, max(1, expires_in - 300))
    return value


def _jsapi_ticket(app_id, secret):
    ticket_key = f"wechat:official-account:jsapi-ticket:{app_id}"
    ticket = cache.get(ticket_key)
    if ticket:
        return ticket
    token = _cached_credential(
        f"wechat:official-account:access-token:{app_id}",
        "access_token",
        Request(
            "https://api.weixin.qq.com/cgi-bin/stable_token",
            data=json.dumps({
                "grant_type": "client_credential",
                "appid": app_id,
                "secret": secret,
                "force_refresh": False,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        ),
    )
    query = urlencode({"access_token": token, "type": "jsapi"})
    return _cached_credential(
        ticket_key, "ticket", Request(f"https://api.weixin.qq.com/cgi-bin/ticket/getticket?{query}"),
    )


def navigation_config(url):
    allowed_origins = {
        (parts.scheme, parts.netloc.lower())
        for configured_url in (
            settings.WECHAT_H5_LOGIN_RETURN_URL,
            settings.WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL,
            settings.PROVIDER_INVITE_H5_BASE_URL,
        )
        if (parts := urlsplit(configured_url.strip())).scheme == "https" and parts.netloc
    }
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValidationError({"url": "微信地图页面地址无效。"}) from exc
    if (
        len(url) > 2048 or "#" in url or any(ch.isspace() for ch in url)
        or (parts.scheme, parts.netloc.lower()) not in allowed_origins
    ):
        raise ValidationError({"url": "微信地图页面地址无效。"})

    app_id = settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip()
    secret = settings.WECHAT_OFFICIAL_ACCOUNT_APP_SECRET.strip()
    if not app_id or not secret:
        raise WechatNavigationUnavailable()
    ticket = _jsapi_ticket(app_id, secret)
    nonce = secrets.token_hex(16)
    timestamp = int(time.time())
    signature_input = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={url}"
    return {
        "appId": app_id,
        "nonceStr": nonce,
        "timestamp": timestamp,
        "signature": hashlib.sha1(signature_input.encode("utf-8"), usedforsecurity=False).hexdigest(),
    }
