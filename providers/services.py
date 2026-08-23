from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderProfile,
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
    if not profile.lifestyle_photo_id:
        missing.append("生活照")
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
