"""Authenticated WeChat linking, separate from login/account creation tickets."""

import secrets
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import ValidationError

from .models import User, WechatLoginIdentity
from .wechat_login import (
    WECHAT_AUTHORIZE_URL,
    WechatIdentity,
    _attach_identity,
    _credentials,
    _exchange_code,
    _h5_urls,
    _ttl,
)

STATE_PREFIX = "wechat:binding:h5:state:"
TICKET_PREFIX = "wechat:binding:ticket:"


def binding_status(user: User) -> dict:
    # Payment authorization is NOT permission to log in as that WeChat identity.
    apps = {
        WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT: settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip(),
        WechatLoginIdentity.Channel.MOBILE_APP: settings.WECHAT_MOBILE_APP_ID.strip(),
    }
    channels = [
        channel for channel, app_id in apps.items()
        if app_id and WechatLoginIdentity.objects.filter(
            user=user, channel=channel, app_id=app_id,
        ).exists()
    ]
    return {"bound": bool(channels), "channels": channels}


def _owner(request) -> dict:
    session_id = str(request.auth.get("session_id", "")) if request.auth else ""
    if not session_id and getattr(request, "session", None) and request.session.session_key:
        session_id = "django:" + salted_hmac("wechat-binding-session", request.session.session_key).hexdigest()
    if not session_id:
        raise ValidationError("请重新登录后再绑定微信。")
    return {
        "user_id": request.user.pk,
        "auth_version": request.user.auth_version,
        "session_id": session_id,
    }


def _lock_owner(request, expected=None) -> User:
    user = User.objects.select_for_update().get(pk=request.user.pk)
    if (
        not user.is_active or user.account_status != User.AccountStatus.ACTIVE
        or user.auth_version != request.user.auth_version
        or (expected is not None and expected != _owner(request))
    ):
        raise ValidationError("账号或登录状态已变更，请重新登录后绑定微信。")
    return user


@transaction.atomic
def begin_h5_binding(request) -> tuple[str, str]:
    _lock_owner(request)
    app_id, _secret = _credentials(WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT)
    callback, _return_url = _h5_urls()
    state = "bind_" + secrets.token_urlsafe(24)
    cache.set(f"{STATE_PREFIX}{state}", _owner(request), _ttl())
    query = urlencode({
        "appid": app_id, "redirect_uri": callback, "response_type": "code",
        "scope": "snsapi_base", "state": state,
    })
    return f"{WECHAT_AUTHORIZE_URL}?{query}#wechat_redirect", state


def _return_url(parameters: dict) -> str:
    _callback, configured_return = _h5_urls()
    parts = urlsplit(configured_return)
    # Fixed, trusted app route; clients cannot supply a redirect or account ID.
    # Preserve the deployed H5 base path for hash routers.
    if parts.fragment:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "",
                           "/pages/profile/edit?" + urlencode(parameters)))
    base_path = parts.path.split("/pages/", 1)[0] if "/pages/" in parts.path else ""
    return urlunsplit((parts.scheme, parts.netloc, base_path + "/pages/profile/edit",
                      urlencode(parameters), ""))


def complete_h5_binding_callback(*, code: str, state: str) -> str:
    _credentials(WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT)
    _h5_urls()
    owner = cache.get(f"{STATE_PREFIX}{state}")
    if not state.startswith("bind_") or not isinstance(owner, dict):
        raise ValidationError("微信绑定授权已失效，请返回资料页重新授权。")
    if not cache.add(f"{STATE_PREFIX}{state}:used", True, _ttl()):
        raise ValidationError("微信绑定授权已使用，请重新授权。")
    cache.delete(f"{STATE_PREFIX}{state}")
    if not code:
        return _return_url({"wechatBindError": "cancelled", "wechatBindState": state})
    identity = _exchange_code(channel=WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT, code=code)
    ticket = secrets.token_urlsafe(32)
    cache.set(f"{TICKET_PREFIX}{ticket}", {"owner": owner, "identity": identity.__dict__}, _ttl())
    return _return_url({"wechatBindTicket": ticket, "wechatBindState": state})


@transaction.atomic
def complete_h5_binding(request, ticket: str) -> dict:
    payload = cache.get(f"{TICKET_PREFIX}{ticket}")
    if not isinstance(payload, dict) or not isinstance(payload.get("owner"), dict) or cache.get(f"{TICKET_PREFIX}{ticket}:used"):
        raise ValidationError("微信绑定凭证已失效，请重新授权。")
    user = _lock_owner(request, payload.get("owner"))
    try:
        identity = WechatIdentity(**payload["identity"])
    except (TypeError, KeyError) as exc:
        raise ValidationError("微信绑定凭证无效，请重新授权。") from exc
    current_app, _secret = _credentials(WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT)
    if identity.channel != WechatLoginIdentity.Channel.OFFICIAL_ACCOUNT or identity.app_id != current_app:
        raise ValidationError("微信绑定配置已变更，请重新授权。")
    if not cache.add(f"{TICKET_PREFIX}{ticket}:used", True, _ttl()):
        raise ValidationError("微信绑定凭证已使用，请重新授权。")
    cache.delete(f"{TICKET_PREFIX}{ticket}")
    _attach_identity(identity=identity, user=user)
    return binding_status(user)


def bind_mobile(request, code: str) -> dict:
    # Exchange the one-use SDK code on the server; never accept OpenID from clients.
    identity = _exchange_code(channel=WechatLoginIdentity.Channel.MOBILE_APP, code=code)
    with transaction.atomic():
        user = _lock_owner(request)
        _attach_identity(identity=identity, user=user)
        return binding_status(user)
