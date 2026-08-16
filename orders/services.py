import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from providers.models import ProviderProfile, ProviderService

from .models import ProviderOrder

MINIMUM_ADVANCE = timedelta(hours=1)
MAXIMUM_ADVANCE = timedelta(days=3)
MINIMUM_HOURLY_MINUTES = 120
TIME_GRAIN_MINUTES = 30
PAYMENT_LOCK_MINUTES = 15


@dataclass(frozen=True)
class PriceQuote:
    service_fee_amount: int
    transport_fee_amount: int
    other_fee_amount: int
    discount_amount: int
    payable_amount: int
    snapshot: dict


def calculate_service_fee(service: ProviderService, duration_minutes: int) -> int:
    if service.billing_type == ProviderService.BillingType.PER_SESSION:
        return service.price_amount
    value = Decimal(service.price_amount) * Decimal(duration_minutes) / Decimal(60)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fallback_transport_fee(distance_km: Decimal | None) -> int:
    if distance_km is None or distance_km <= 0:
        return 0
    # PRD initial fallback: CNY 5 within 5 km, then CNY 5 for each additional 5 km.
    return max(1, math.ceil(float(distance_km) / 5)) * 500


def build_quote(service: ProviderService, duration_minutes: int, distance_km: Decimal | None) -> PriceQuote:
    service_fee = calculate_service_fee(service, duration_minutes)
    transport_fee = fallback_transport_fee(distance_km)
    total = service_fee + transport_fee
    return PriceQuote(
        service_fee_amount=service_fee,
        transport_fee_amount=transport_fee,
        other_fee_amount=0,
        discount_amount=0,
        payable_amount=total,
        snapshot={
            "version": "provider-order-pricing-v1",
            "currency": "CNY",
            "time_grain_minutes": TIME_GRAIN_MINUTES,
            "minimum_hourly_minutes": MINIMUM_HOURLY_MINUTES,
            "transport_rule": "fallback_5_cny_per_5km" if distance_km is not None else "pending_map_route",
            "coupon": None,
        },
    )


def validate_booking(service: ProviderService, starts_at, duration_minutes: int):
    now = timezone.now()
    if starts_at < now + MINIMUM_ADVANCE:
        raise ValidationError({"starts_at": "至少提前1小时预约。"})
    if starts_at > now + MAXIMUM_ADVANCE:
        raise ValidationError({"starts_at": "最远仅可预约未来3天。"})
    if starts_at.minute % TIME_GRAIN_MINUTES or starts_at.second or starts_at.microsecond:
        raise ValidationError({"starts_at": "开始时间必须按30分钟粒度选择。"})
    if duration_minutes % TIME_GRAIN_MINUTES:
        raise ValidationError({"duration_minutes": "服务时长必须按30分钟递增。"})
    if service.billing_type == ProviderService.BillingType.HOURLY:
        if duration_minutes < MINIMUM_HOURLY_MINUTES:
            raise ValidationError({"duration_minutes": "按小时服务最低预约2小时。"})
    elif service.estimated_duration_minutes:
        duration_minutes = service.estimated_duration_minutes
    return duration_minutes, starts_at + timedelta(minutes=duration_minutes)


def ensure_slot_available(provider: ProviderProfile, starts_at, ends_at):
    now = timezone.now()
    blocking = Q(status=ProviderOrder.Status.PENDING_PAYMENT, payment_expires_at__gt=now) | Q(
        status__in=(
            ProviderOrder.Status.PENDING_ACCEPTANCE,
            ProviderOrder.Status.PENDING_SUPPORT,
            ProviderOrder.Status.PENDING_SERVICE,
            ProviderOrder.Status.DEPARTED,
            ProviderOrder.Status.IN_SERVICE,
            ProviderOrder.Status.PENDING_CONFIRMATION,
        )
    )
    if ProviderOrder.objects.filter(
        blocking, provider=provider, starts_at__lt=ends_at, ends_at__gt=starts_at
    ).exists():
        raise ValidationError({"starts_at": "该时间段刚刚被预约，请选择其他时间。"})


def expire_pending_orders(queryset=None) -> int:
    queryset = queryset if queryset is not None else ProviderOrder.objects.all()
    now = timezone.now()
    return queryset.filter(
        status=ProviderOrder.Status.PENDING_PAYMENT,
        payment_expires_at__lte=now,
    ).update(status=ProviderOrder.Status.CANCELLED, cancelled_at=now, updated_at=now)
