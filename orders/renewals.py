from copy import copy
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.account_closure import lock_active_user_for_business
from accounts.models import UserFunctionRestriction
from backoffice.operation_settings import platform_operation_rules
from locations.discovery import open_service_cities
from providers.availability import booking_window_end
from providers.models import ProviderProfile
from taskcenter.services import register_provider_order_payment_expiry
from wallets.lots import best_wallet_discount_rate
from wallets.models import UserWallet
from wallets.services import prepare_wallet_payment

from .booking_slots import blocking_orders
from .cancellations import DEFAULTS
from .commission import commission_snapshot
from .models import ProviderOrder
from .services import TIME_GRAIN_MINUTES, build_quote, create_provider_order_payment_order
from .timeouts import enroll_fulfillment_timeouts


STATUS = ProviderOrder.Status
SOURCE_FINISHED = (STATUS.PENDING_CONFIRMATION, STATUS.PENDING_REVIEW, STATUS.COMPLETED)


def existing_renewal(source, now):
    return (
        source.renewals.exclude(status__in=(STATUS.CANCELLED, STATUS.REFUNDED))
        .exclude(status=STATUS.PENDING_PAYMENT, payment_expires_at__lte=now)
        .first()
    )


def source_can_renew(source, now):
    return (
        source.status == STATUS.IN_SERVICE
        and source.service_started_at is not None
        and source.ends_at > now
        and not source.fulfillment_review_required
    )


def renewal_source_allows_payment(order, *, lock_provider=False):
    if not order.renewal_of_id:
        return True
    parents = ProviderOrder.objects
    if lock_provider:
        parents = parents.select_for_update()
    source = parents.only("status", "service_started_at", "fulfillment_review_required").get(pk=order.renewal_of_id)
    if lock_provider:
        ProviderProfile.objects.select_for_update().only("pk").get(pk=order.provider_id)
    return (
        timezone.now() < order.payment_expires_at
        and source.service_started_at is not None
        and source.status in (STATUS.IN_SERVICE, *SOURCE_FINISHED)
        and not source.fulfillment_review_required
    )


def allowed_durations(source, travel_minutes, now):
    if not source_can_renew(source, now):
        return []
    provider, service = source.provider, source.service
    if (
        provider.status != ProviderProfile.Status.APPROVED
        or provider.onboarding_status != ProviderProfile.OnboardingStatus.APPROVED
        or provider.identity_status != ProviderProfile.IdentityStatus.VERIFIED
        or provider.content_hidden
        or not service.is_active
        or service.content_hidden
        or not service.category.is_active
        or provider.service_city_code not in open_service_cities()
    ):
        return []
    starts_at = source.ends_at
    window_end = booking_window_end(provider, starts_at)
    if not window_end:
        return []

    # Earlier segments reserve travel for outside bookings, but the same customer can extend again.
    chain = [source.pk]
    parent_id = source.renewal_of_id
    while parent_id is not None:
        chain.append(parent_id)
        parent_id = ProviderOrder.objects.values_list("renewal_of_id", flat=True).get(pk=parent_id)

    travel = timedelta(minutes=travel_minutes)
    next_booking_start = (
        blocking_orders(provider, starts_at, window_end + travel, now=now)
        .exclude(pk__in=chain).order_by("starts_at").values_list("starts_at", flat=True).first()
    )
    if next_booking_start is not None:
        window_end = min(window_end, next_booking_start - travel)
    available_minutes = (window_end - starts_at) // timedelta(minutes=1)
    if source.billing_type_snapshot == ProviderOrder.BillingType.PER_SESSION:
        return [source.duration_minutes] if source.duration_minutes <= available_minutes else []
    return list(range(TIME_GRAIN_MINUTES, min(480, available_minutes) + 1, TIME_GRAIN_MINUTES))


def renewal_quote(source, duration, rate, *, commission=None):
    service = copy(source.service)
    service.provider = source.provider
    service.billing_type = source.billing_type_snapshot
    service.price_amount = source.unit_price_amount
    quote = build_quote(service, duration, None, wallet_discount_rate_bps=rate, commission=commission)
    quote.snapshot.update(transport_rule="renewal_no_travel", minimum_hourly_minutes=TIME_GRAIN_MINUTES)
    return quote


def renewal_options(source):
    now = timezone.now()
    child = existing_renewal(source, now)
    if child:
        return {"existing_order_no": child.order_no, "existing_order_status": child.status, "options": []}
    travel = platform_operation_rules()["provider_order_renewal_travel_minutes"]
    durations = allowed_durations(source, travel, now)
    wallet = UserWallet.objects.filter(user_id=source.customer_id).first() if durations else None
    rate = best_wallet_discount_rate(wallet) if wallet else 10000
    commission = commission_snapshot(source.provider, source.service.category.platform_commission_rate) if durations else None
    return {
        "starts_at": source.ends_at,
        "travel_buffer_minutes": travel,
        "options": [
            {
                "duration_minutes": duration,
                "ends_at": source.ends_at + timedelta(minutes=duration),
                "payable_amount": renewal_quote(source, duration, rate, commission=commission).payable_amount,
            }
            for duration in durations
        ],
    }


@transaction.atomic
def create_renewal(source, *, user, duration, expected_payable, order_no):
    customer = lock_active_user_for_business(user, function=UserFunctionRestriction.Function.PROVIDER_ORDER)
    # Load the service only after the provider lock so revocations are visible.
    source = ProviderOrder.objects.select_for_update().get(pk=source.pk)
    provider = ProviderProfile.objects.select_for_update().get(pk=source.provider_id)
    source.provider = provider
    now = timezone.now()
    child = existing_renewal(source, now)
    if child:
        if child.duration_minutes == duration and child.payable_amount == expected_payable:
            return child, False
        raise ValidationError("该订单已有续单，请查看续单详情。")

    rules = platform_operation_rules()
    travel = rules["provider_order_renewal_travel_minutes"]
    if duration not in allowed_durations(source, travel, now):
        raise ValidationError("该时段已无法续单，请刷新可续时长。")

    wallet = UserWallet.objects.select_for_update().filter(user_id=customer.pk).first()
    rate = best_wallet_discount_rate(wallet) if wallet else 10000
    quote = renewal_quote(source, duration, rate)
    if quote.payable_amount != expected_payable:
        raise ValidationError("续单金额已变化，请重新确认。")

    ends_at = source.ends_at + timedelta(minutes=duration)
    order = ProviderOrder.objects.create(
        order_no=order_no,
        renewal_of=source,
        renewal_travel_until=ends_at + timedelta(minutes=travel),
        customer=customer,
        provider=provider,
        service=source.service,
        provider_name_snapshot=source.provider_name_snapshot,
        service_name_snapshot=source.service_name_snapshot,
        billing_type_snapshot=source.billing_type_snapshot,
        unit_price_amount=source.unit_price_amount,
        starts_at=source.ends_at,
        ends_at=ends_at,
        duration_minutes=duration,
        meeting_location_name=source.meeting_location_name,
        meeting_address=source.meeting_address,
        source_longitude=source.source_longitude,
        source_latitude=source.source_latitude,
        map_source=source.map_source,
        contact_name=source.contact_name,
        contact_gender=source.contact_gender,
        contact_phone=source.contact_phone,
        service_fee_amount=quote.service_fee_amount,
        discount_amount=quote.discount_amount,
        payable_amount=quote.payable_amount,
        pricing_snapshot=quote.snapshot,
        cancellation_policy={
            "version": "provider-order-renewal-v1",
            "config": DEFAULTS,
            "clauses": ["续单开始前取消，退还续单实付金额。服务开始后，因个人原因提前结束，需承担续单服务费。"],
            "agreed_at": now.isoformat(),
        },
        payment_expires_at=min(source.ends_at, now + timedelta(minutes=rules["provider_order_payment_timeout_minutes"])),
    )
    enroll_fulfillment_timeouts(order)
    create_provider_order_payment_order(order)
    if rate < 10000:
        prepare_wallet_payment(
            user_id=customer.pk, business_type="provider_order",
            business_order_no=order.order_no, payable_amount=order.payable_amount,
        )
    register_provider_order_payment_expiry(order)
    return order, True
