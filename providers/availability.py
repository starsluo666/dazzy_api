from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from orders.models import ProviderOrder
from orders.services import MAXIMUM_ADVANCE, MINIMUM_ADVANCE, TIME_GRAIN_MINUTES

from .models import (
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderProfile,
    ProviderWeeklyAvailability,
)


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
            ProviderOrder.Status.AFTER_SALES,
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
    if (
        not provider.is_accepting_orders
        or provider.admin_order_restricted
        or provider.date_closures.filter(date=local_start.date()).exists()
    ):
        return False
    weekly = provider.weekly_availability.filter(
        is_active=True,
        weekday=local_start.weekday(),
        starts_at__lte=local_start.time().replace(tzinfo=None),
        ends_at__gte=local_end.time().replace(tzinfo=None),
    ).exists()
    extra = provider.date_availability.filter(
        date=local_start.date(),
        starts_at__lte=local_start.time().replace(tzinfo=None),
        ends_at__gte=local_end.time().replace(tzinfo=None),
    ).exists()
    return weekly or extra


def ensure_booking_within_schedule(provider, starts_at, ends_at):
    if not booking_is_within_schedule(provider, starts_at, ends_at):
        raise ValidationError({"starts_at": "所选时间不在达人的可服务时段内。"})


def build_availability(provider: ProviderProfile, start_date: date, days: int, duration: int):
    now = timezone.now()
    latest_start = now + MAXIMUM_ADVANCE
    range_start = _local_datetime(start_date, time.min)
    range_end = _local_datetime(start_date + timedelta(days=days), time.min)
    schedules = list(
        ProviderWeeklyAvailability.objects.filter(provider=provider, is_active=True).order_by(
            "weekday", "starts_at"
        )
    )
    extras = list(
        ProviderDateAvailability.objects.filter(
            provider=provider, date__gte=start_date, date__lt=start_date + timedelta(days=days)
        ).order_by("date", "starts_at")
    )
    closures = set(
        ProviderDateClosure.objects.filter(
            provider=provider, date__gte=start_date, date__lt=start_date + timedelta(days=days)
        ).values_list("date", flat=True)
    )
    orders = _blocking_orders(provider, range_start, range_end)
    result = []
    earliest = None
    step = timedelta(minutes=TIME_GRAIN_MINUTES)
    service_duration = timedelta(minutes=duration)

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        slots = []
        slot_keys = set()
        if day in closures or not provider.is_accepting_orders or provider.admin_order_restricted:
            result.append({"date": day, "slots": slots})
            continue
        periods = [schedule for schedule in schedules if schedule.weekday == day.weekday()]
        periods += [schedule for schedule in extras if schedule.date == day]
        for schedule in periods:
            cursor = _local_datetime(day, schedule.starts_at)
            boundary = _local_datetime(day, schedule.ends_at)
            while cursor + service_duration <= boundary:
                slot_end = cursor + service_duration
                conflict = any(
                    order_start < slot_end and order_end > cursor
                    for order_start, order_end in orders
                )
                slot_key = (cursor, slot_end)
                if (
                    cursor >= now + MINIMUM_ADVANCE
                    and cursor <= latest_start
                    and not conflict
                    and slot_key not in slot_keys
                ):
                    payload = {"starts_at": cursor, "ends_at": slot_end}
                    slots.append(payload)
                    slot_keys.add(slot_key)
                cursor += step
        slots.sort(key=lambda item: item["starts_at"])
        if slots and (earliest is None or slots[0]["starts_at"] < earliest["starts_at"]):
            earliest = slots[0]
        result.append({"date": day, "slots": slots})
    return {"earliest": earliest, "dates": result}
