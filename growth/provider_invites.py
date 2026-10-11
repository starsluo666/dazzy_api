import base64
import io
from urllib.parse import urlsplit

import qrcode
from django.conf import settings
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError

from providers.models import ProviderProfile
from .models import ProviderInviteConfig


def invite_h5_base():
    # No customer-app fallback: the registration page belongs to the provider app.
    base = str(settings.PROVIDER_INVITE_H5_BASE_URL or "").strip().rstrip("/")
    try:
        URLValidator(schemes=["https"])(base)
        parsed = urlsplit(base)
        if "?" in base or "#" in base or parsed.username or parsed.password:
            return ""
    except (DjangoValidationError, ValueError):
        return ""
    return base


def require_invite_h5_base():
    base = invite_h5_base()
    if not base:
        raise ValidationError("请先配置承载达人邀请页的用户端 H5 地址 PROVIDER_INVITE_H5_BASE_URL（HTTPS，不含查询参数或 #）。")
    return base


def service_cities():
    from locations.discovery import discovery_cities

    return {row["city_code"]: row["city_name"] for row in discovery_cities()}


def config_payload(config=None):
    config = config or ProviderInviteConfig.objects.filter(pk=1).first()
    return {
        key: getattr(config, key, default)
        for key, default in (
            ("enabled", False),
            ("store_reward_amount", 0),
            ("provider_reward_amount", 0),
            ("revision", 0),
        )
    }


def source_available(source):
    if not source.active:
        return False
    if source.kind == "provider":
        user = source.provider_user
        return (
            user.is_active
            and user.account_status == "active"
            and ProviderProfile.objects.filter(user=user, status="approved").exists()
        )
    return True


def require_available(source, config):
    require_invite_h5_base()
    if not config.enabled or not source_available(source):
        raise ValidationError("该邀请暂不可用，请联系邀请人或平台。")


def source_payload(source):
    # The base is deployment configuration, never derived from untrusted Host or input URLs.
    base = invite_h5_base()
    return {
        "public_id": str(source.public_id),
        "code": source.code,
        "kind": source.kind,
        "name": source.name,
        "active": source.active,
        "revision": source.revision,
        "invite_url": f"{base}/#/pages/invitations/provider-register?code={source.code}" if base else "",
    }


def qr_data(source):
    require_invite_h5_base()
    buffer = io.BytesIO()
    qrcode.make(source_payload(source)["invite_url"]).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def reward_payload(reward, *, admin=False):
    phone = reward.invitee.phone
    data = {
        "public_id": str(reward.public_id),
        "amount": reward.amount,
        "status": reward.status,
        "status_label": reward.get_status_display(),
        "created_at": reward.created_at,
        "paid_at": reward.paid_at,
        "invitee_phone_masked": f"{phone[:3]}****{phone[-4:]}",
    }
    if admin:
        data.update(
            source=source_payload(reward.source),
            source_name_snapshot=reward.source_name_snapshot,
            code_snapshot=reward.code_snapshot,
            profile_id=reward.profile_id,
            invitee_public_id=str(reward.invitee.public_id),
            application_status=reward.profile.status,
            real_name=reward.profile.application_real_name,
            service_city_name=reward.profile.service_city_name,
            review_note=reward.review_note,
            reviewed_at=reward.reviewed_at,
            reviewed_by=str(reward.reviewed_by.public_id) if reward.reviewed_by_id else None,
            paid_by=str(reward.paid_by.public_id) if reward.paid_by_id else None,
            transfer_reference=reward.transfer_reference,
            payout_note=reward.payout_note,
            revision=reward.revision,
            config_revision=reward.config_revision,
        )
    return data
