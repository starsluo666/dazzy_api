import hashlib
import hmac
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from config.geospatial import gcj02_to_wgs84

from .models import (
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderLiveLocation,
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
)


@transaction.atomic
def save_provider_application(*, user, data) -> ProviderProfile:
    profile, _ = ProviderProfile.objects.select_for_update().get_or_create(user=user)
    if profile.status in (ProviderProfile.Status.PENDING, ProviderProfile.Status.APPROVED):
        raise ValidationError("当前状态不可修改申请资料。")
    for field, value in data.items():
        setattr(profile, field, value)
    if profile.status == ProviderProfile.Status.REJECTED:
        profile.status = ProviderProfile.Status.DRAFT
        profile.rejection_reason = ""
        profile.reviewed_at = None
    profile.save()
    return profile


@transaction.atomic
def submit_provider_application(*, user) -> ProviderProfile:
    profile = ProviderProfile.objects.select_for_update().filter(user=user).first()
    if profile is None:
        raise ValidationError("请先填写达人申请资料。")
    if profile.status not in (ProviderProfile.Status.DRAFT, ProviderProfile.Status.REJECTED):
        raise ValidationError("当前申请状态不可重复提交。")
    missing = []
    if len(profile.bio.strip()) < 10:
        missing.append("达人简介")
    if not profile.service_city_code or not profile.service_city_name:
        missing.append("服务城市")
    if missing:
        raise ValidationError({"detail": f"请先完善：{'、'.join(missing)}。"})
    now = timezone.now()
    profile.status = ProviderProfile.Status.PENDING
    profile.agreement_accepted_at = now
    profile.submitted_at = now
    profile.rejection_reason = ""
    profile.save(
        update_fields=(
            "status",
            "agreement_accepted_at",
            "submitted_at",
            "rejection_reason",
            "updated_at",
        )
    )
    return profile


def provider_profile_blockers(profile: ProviderProfile) -> list[str]:
    blockers = []
    if profile.identity_status != ProviderProfile.IdentityStatus.VERIFIED:
        blockers.append("请先完成实名认证")
    if not profile.is_profile_complete:
        blockers.append("请先完善达人资料")
    if not ProviderService.objects.filter(
        provider=profile,
        is_active=True,
        category__is_active=True,
    ).exists():
        blockers.append("请先添加并启用至少一项服务")
    if profile.admin_order_restricted:
        blockers.append(profile.admin_restriction_reason or "平台当前限制接单")
    return blockers


@transaction.atomic
def save_provider_identity(*, provider: ProviderProfile, data: dict) -> ProviderProfile:
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    if locked.identity_status in (
        ProviderProfile.IdentityStatus.PENDING,
        ProviderProfile.IdentityStatus.VERIFIED,
    ):
        raise ValidationError({"detail": "当前实名认证状态不可修改。"})
    id_number = data.pop("id_number", "")
    for field, value in data.items():
        setattr(locked, field, value)
    if id_number:
        normalized = id_number.strip().upper()
        if not re.fullmatch(r"\d{17}[0-9X]", normalized):
            raise ValidationError({"id_number": "请输入有效的18位身份证号码。"})
        locked.identity_number_masked = f"{normalized[:4]}**********{normalized[-4:]}"
        locked.identity_number_digest = hmac.new(
            settings.SECRET_KEY.encode(), normalized.encode(), hashlib.sha256
        ).hexdigest()
    if locked.identity_status == ProviderProfile.IdentityStatus.REJECTED:
        locked.identity_status = ProviderProfile.IdentityStatus.UNVERIFIED
        locked.identity_rejection_reason = ""
        locked.identity_reviewed_at = None
    locked.save()
    return locked


@transaction.atomic
def submit_provider_identity(*, provider: ProviderProfile) -> ProviderProfile:
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    if locked.identity_status not in (
        ProviderProfile.IdentityStatus.UNVERIFIED,
        ProviderProfile.IdentityStatus.REJECTED,
    ):
        raise ValidationError({"detail": "当前实名认证状态不可重复提交。"})
    missing = []
    if not locked.identity_real_name.strip():
        missing.append("真实姓名")
    if not locked.identity_number_digest:
        missing.append("身份证号码")
    if not locked.identity_front_photo_id:
        missing.append("身份证人像面")
    if not locked.identity_back_photo_id:
        missing.append("身份证国徽面")
    if not locked.identity_face_photo_id:
        missing.append("本人核验照片")
    if missing:
        raise ValidationError({"detail": f"请先完善：{'、'.join(missing)}。"})
    locked.identity_status = ProviderProfile.IdentityStatus.PENDING
    locked.identity_submitted_at = timezone.now()
    locked.identity_reviewed_at = None
    locked.identity_rejection_reason = ""
    locked.is_accepting_orders = False
    locked.save(
        update_fields=(
            "identity_status",
            "identity_submitted_at",
            "identity_reviewed_at",
            "identity_rejection_reason",
            "is_accepting_orders",
            "updated_at",
        )
    )
    return locked


def _location_values(data: dict, *, session_id, now) -> dict:
    longitude = data["longitude"]
    latitude = data["latitude"]
    return {
        "session_id": session_id,
        "source_longitude": longitude,
        "source_latitude": latitude,
        "position": gcj02_to_wgs84(longitude, latitude),
        "accuracy_m": data["accuracy_m"],
        "speed_mps": data.get("speed_mps"),
        "located_at": data.get("located_at") or now,
        "received_at": now,
    }


@transaction.atomic
def start_provider_online(
    *, provider: ProviderProfile, data: dict
) -> tuple[ProviderProfile, ProviderLiveLocation]:
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    blockers = provider_profile_blockers(locked)
    if blockers:
        raise ValidationError({"detail": f"{blockers[0]}。"})

    now = timezone.now()
    session_id = uuid.uuid4()
    location, _ = ProviderLiveLocation.objects.update_or_create(
        provider=locked,
        defaults=_location_values(data, session_id=session_id, now=now),
    )
    locked.is_accepting_orders = True
    locked.save(update_fields=("is_accepting_orders", "updated_at"))
    return locked, location


@transaction.atomic
def update_provider_live_location(
    *, provider: ProviderProfile, session_id, data: dict
) -> tuple[ProviderProfile, ProviderLiveLocation]:
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    if not locked.is_accepting_orders or locked.admin_order_restricted:
        raise ValidationError({"detail": "当前接单会话已停止，请重新开启接单。"})
    try:
        location = ProviderLiveLocation.objects.select_for_update().get(provider=locked)
    except ProviderLiveLocation.DoesNotExist as exc:
        raise ValidationError({"detail": "接单会话不存在，请重新开启接单。"}) from exc
    if location.session_id != session_id:
        raise ValidationError({"session_id": "接单会话已失效，请重新开启接单。"})

    now = timezone.now()
    for field, value in _location_values(data, session_id=session_id, now=now).items():
        setattr(location, field, value)
    location.save()
    return locked, location


@transaction.atomic
def stop_provider_online(*, provider: ProviderProfile) -> ProviderProfile:
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    locked.is_accepting_orders = False
    locked.save(update_fields=("is_accepting_orders", "updated_at"))
    ProviderLiveLocation.objects.filter(provider=locked).update(session_id=None)
    return locked


def _ensure_no_overlap(queryset, *, starts_at, ends_at, field: str) -> None:
    overlapping = queryset.filter(starts_at__lt=ends_at, ends_at__gt=starts_at)
    duplicate = overlapping.filter(starts_at=starts_at, ends_at=ends_at).exists()
    if overlapping.exclude(starts_at=starts_at, ends_at=ends_at).exists():
        raise ValidationError({field: "该时间段与已有可预约时段重叠。"})
    if duplicate:
        return


@transaction.atomic
def create_provider_schedule_periods(*, provider: ProviderProfile, data: dict) -> list[str]:
    ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    starts_at = data["starts_at"]
    ends_at = data["ends_at"]
    created: list[str] = []

    weekdays = set(data["copy_weekdays"])
    if data["repeat_weekly"]:
        weekdays.add(data["date"].weekday())
    for weekday in weekdays:
        queryset = ProviderWeeklyAvailability.objects.filter(
            provider=provider,
            weekday=weekday,
            is_active=True,
        )
        _ensure_no_overlap(
            queryset,
            starts_at=starts_at,
            ends_at=ends_at,
            field="starts_at",
        )
        item, _ = ProviderWeeklyAvailability.objects.get_or_create(
            provider=provider,
            weekday=weekday,
            starts_at=starts_at,
            ends_at=ends_at,
            defaults={"is_active": True},
        )
        if not item.is_active:
            item.is_active = True
            item.save(update_fields=("is_active", "updated_at"))
        created.append(f"weekly-{item.id}")

    if not data["repeat_weekly"]:
        queryset = ProviderDateAvailability.objects.filter(
            provider=provider,
            date=data["date"],
        )
        _ensure_no_overlap(
            queryset,
            starts_at=starts_at,
            ends_at=ends_at,
            field="starts_at",
        )
        item, _ = ProviderDateAvailability.objects.get_or_create(
            provider=provider,
            date=data["date"],
            starts_at=starts_at,
            ends_at=ends_at,
        )
        created.append(f"date-{item.id}")

    ProviderDateClosure.objects.filter(provider=provider, date=data["date"]).delete()
    return created
