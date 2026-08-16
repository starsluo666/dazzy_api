from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from orders.models import ProviderOrder
from orders.services import MINIMUM_ADVANCE, TIME_GRAIN_MINUTES

from .models import ProviderProfile, ProviderWeeklyAvailability


def _blocking_orders(provider, range_start, range_end):
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
    return list(
        ProviderOrder.objects.filter(
            blocking,
            provider=provider,
            starts_at__lt=range_end,
            ends_at__gt=range_start,
        ).values_list("starts_at", "ends_at")
    )


def _local_datetime(day: date, value: time):
    return timezone.make_aware(datetime.combine(day, value), timezone.get_current_timezone())


def booking_is_within_schedule(provider, starts_at, ends_at) -> bool:
    local_start = timezone.localtime(starts_at)
    local_end = timezone.localtime(ends_at)
    if local_start.date() != local_end.date():
        return False
    return provider.weekly_availability.filter(
        is_active=True,
        weekday=local_start.weekday(),
        starts_at__lte=local_start.time().replace(tzinfo=None),
        ends_at__gte=local_end.time().replace(tzinfo=None),
    ).exists()


def ensure_booking_within_schedule(provider, starts_at, ends_at):
    if not booking_is_within_schedule(provider, starts_at, ends_at):
        raise ValidationError({"starts_at": "所选时间不在达人的可服务时段内。"})


def build_availability(provider: ProviderProfile, start_date: date, days: int, duration: int):
    now = timezone.now()
    range_start = _local_datetime(start_date, time.min)
    range_end = _local_datetime(start_date + timedelta(days=days), time.min)
    schedules = list(
        ProviderWeeklyAvailability.objects.filter(provider=provider, is_active=True).order_by(
            "weekday", "starts_at"
        )
    )
    orders = _blocking_orders(provider, range_start, range_end)
    result = []
    earliest = None
    step = timedelta(minutes=TIME_GRAIN_MINUTES)
    service_duration = timedelta(minutes=duration)

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        slots = []
        for schedule in schedules:
            if schedule.weekday != day.weekday():
                continue
            cursor = _local_datetime(day, schedule.starts_at)
            boundary = _local_datetime(day, schedule.ends_at)
            while cursor + service_duration <= boundary:
                slot_end = cursor + service_duration
                conflict = any(order_start < slot_end and order_end > cursor for order_start, order_end in orders)
                if cursor >= now + MINIMUM_ADVANCE and not conflict:
                    payload = {"starts_at": cursor, "ends_at": slot_end}
                    slots.append(payload)
                    earliest = earliest or payload
                cursor += step
        result.append({"date": day, "slots": slots})
    return {"earliest": earliest, "dates": result}
