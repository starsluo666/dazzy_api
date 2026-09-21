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
    ProviderCategoryGrant,
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderLiveLocation,
    ProviderProfile,
    ProviderProfileRevision,
    ProviderService,
    ProviderServiceRevision,
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
    if not profile.application_real_name.strip():
        missing.append("真实姓名")
    if not profile.application_birth_date:
        missing.append("出生日期")
    elif (
        timezone.localdate().year
        - profile.application_birth_date.year
        - (
            (timezone.localdate().month, timezone.localdate().day)
            < (profile.application_birth_date.month, profile.application_birth_date.day)
        )
        < 18
    ):
        raise ValidationError({"application_birth_date": "申请达人需年满18周岁。"})
    if not profile.lifestyle_photo_id:
        missing.append("近期生活照")
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


def maybe_submit_provider_onboarding(*, provider: ProviderProfile) -> bool:
    """Move a provider into the single combined onboarding queue exactly once."""
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    if locked.onboarding_status in (
        ProviderProfile.OnboardingStatus.APPROVED,
        ProviderProfile.OnboardingStatus.PENDING_REVIEW,
    ):
        return False
    identity_ready = locked.identity_status == ProviderProfile.IdentityStatus.PENDING
    profile_ready = locked.profile_revisions.filter(
        status=ProviderProfileRevision.Status.PENDING
    ).exists()
    service_ready = locked.service_revisions.filter(
        status=ProviderServiceRevision.Status.PENDING
    ).exists()
    if not (identity_ready and profile_ready and service_ready):
        return False
    locked.onboarding_status = ProviderProfile.OnboardingStatus.PENDING_REVIEW
    locked.onboarding_submitted_at = timezone.now()
    locked.onboarding_reviewed_at = None
    locked.onboarding_reviewed_by = None
    locked.onboarding_rejection_reason = ""
    locked.is_accepting_orders = False
    locked.save(
        update_fields=(
            "onboarding_status",
            "onboarding_submitted_at",
            "onboarding_reviewed_at",
            "onboarding_reviewed_by",
            "onboarding_rejection_reason",
            "is_accepting_orders",
            "updated_at",
        )
    )
    return True


@transaction.atomic
def submit_provider_profile_revision(*, provider: ProviderProfile, data: dict) -> ProviderProfileRevision:
    locked = ProviderProfile.objects.select_for_update().select_related("user").get(pk=provider.pk)
    if locked.profile_revisions.filter(status=ProviderProfileRevision.Status.PENDING).exists():
        raise ValidationError({"detail": "达人资料正在审核中，请等待审核结果。"})
    display_name = data.get("display_name", locked.display_name or locked.user.nickname).strip()
    bio = data.get("bio", locked.bio).strip()
    lifestyle_photo = data.get("lifestyle_photo", locked.lifestyle_photo)
    service_city_code = data.get("service_city_code", locked.service_city_code).strip()
    service_city_name = data.get("service_city_name", locked.service_city_name).strip()
    missing = []
    if len(display_name) < 2:
        missing.append("达人名称")
    if len(bio) < 10:
        missing.append("达人简介")
    if not lifestyle_photo:
        missing.append("生活照")
    if not service_city_code or not service_city_name:
        missing.append("服务城市")
    if missing:
        raise ValidationError({"detail": f"请先完善：{'、'.join(missing)}。"})
    revision = ProviderProfileRevision.objects.create(
        provider=locked,
        display_name=display_name,
        bio=bio,
        lifestyle_photo=lifestyle_photo,
        service_city_code=service_city_code,
        service_city_name=service_city_name,
        max_service_radius_km=data.get(
            "max_service_radius_km", locked.max_service_radius_km
        ),
    )
    maybe_submit_provider_onboarding(provider=locked)
    return revision


@transaction.atomic
def submit_provider_service_revision(
    *, provider: ProviderProfile, data: dict, service: ProviderService | None = None
) -> ProviderServiceRevision:
    locked = ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    category = data.get("category", service.category if service else None)
    billing_type = data.get("billing_type", service.billing_type if service else None)
    if not ProviderCategoryGrant.objects.filter(
        provider=locked, category=category, is_active=True
    ).exists():
        raise ValidationError({"category_id": "你尚未获得该服务分类的经营权限。"})
    if service is None:
        service = ProviderService.objects.filter(
            provider=locked, category=category, billing_type=billing_type
        ).first()
    if locked.service_revisions.filter(
        category=category,
        billing_type=billing_type,
        status=ProviderServiceRevision.Status.PENDING,
    ).exists():
        raise ValidationError({"detail": "该服务已有待审核变更，请等待审核结果。"})
    action = (
        ProviderServiceRevision.Action.CREATE
        if service is None
        else ProviderServiceRevision.Action.REACTIVATE
        if not service.is_active
        else ProviderServiceRevision.Action.UPDATE
    )
    revision = ProviderServiceRevision.objects.create(
        provider=locked,
        service=service,
        category=category,
        action=action,
        billing_type=billing_type,
        price_amount=data.get("price_amount", service.price_amount if service else None),
        estimated_duration_minutes=data.get(
            "estimated_duration_minutes",
            service.estimated_duration_minutes if service else None,
        ),
        description=data.get("description", service.description if service else "").strip(),
    )
    maybe_submit_provider_onboarding(provider=locked)
    return revision


@transaction.atomic
def disable_provider_service(*, provider: ProviderProfile, service: ProviderService) -> None:
    ProviderProfile.objects.select_for_update().get(pk=provider.pk)
    service.is_active = False
    service.save(update_fields=("is_active", "updated_at"))
    ProviderServiceRevision.objects.filter(
        provider=provider,
        service=service,
        status=ProviderServiceRevision.Status.PENDING,
    ).update(
        status=ProviderServiceRevision.Status.REJECTED,
        reviewed_at=timezone.now(),
        rejection_reason="达人已下架该服务，本次变更自动终止。",
    )


def provider_profile_blockers(profile: ProviderProfile) -> list[str]:
    blockers = []
    if profile.onboarding_status != ProviderProfile.OnboardingStatus.APPROVED:
        blockers.append("请等待达人开通审核通过")
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
    if (
        locked.application_real_name
        and locked.identity_real_name.strip() != locked.application_real_name.strip()
    ):
        raise ValidationError({"identity_real_name": "实名认证姓名必须与入驻申请姓名一致。"})
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
    maybe_submit_provider_onboarding(provider=locked)
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
