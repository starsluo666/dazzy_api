import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Avg, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from notifications.models import UserNotification
from notifications.services import create_notification, create_order_notification

from providers.models import ProviderProfile, ProviderService

from .models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderSettlement,
)

MINIMUM_ADVANCE = timedelta(hours=1)
MAXIMUM_ADVANCE = timedelta(days=3)
MINIMUM_HOURLY_MINUTES = 120
TIME_GRAIN_MINUTES = 30
PAYMENT_LOCK_MINUTES = 15


def create_provider_order_payment_order(order: ProviderOrder):
    return ProviderOrderPaymentOrder.objects.get_or_create(
        order=order,
        defaults={
            "payer": order.customer,
            "service_fee_amount": order.service_fee_amount,
            "transport_fee_amount": order.transport_fee_amount,
            "other_fee_amount": order.other_fee_amount,
            "discount_amount": order.discount_amount,
            "payable_amount": order.payable_amount,
            "pricing_snapshot": order.pricing_snapshot,
            "expires_at": order.payment_expires_at,
        },
    )


@transaction.atomic
def apply_provider_order_payment_success(
    *, order_no: str, customer_id: int, channel: str, gateway_trade_no: str, now=None
):
    from providers.presence import operation_rules
    from taskcenter.services import (
        cancel_provider_order_payment_expiry,
        mark_provider_order_payment_expired,
        register_provider_acceptance_timeout,
    )

    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update().select_related("customer").get(
        order_no=order_no,
        customer_id=customer_id,
    )
    payment, _ = create_provider_order_payment_order(order)
    payment = ProviderOrderPaymentOrder.objects.select_for_update().get(pk=payment.pk)
    if (
        payment.status
        in (
            ProviderOrderPaymentOrder.Status.PAID,
            ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
            ProviderOrderPaymentOrder.Status.REFUNDED,
        )
        and order.paid_at
    ):
        return order, payment, False
    if order.status != ProviderOrder.Status.PENDING_PAYMENT:
        raise ValidationError({"status": "订单不在待支付状态。"})
    if order.payment_expires_at <= now:
        order.status = ProviderOrder.Status.CANCELLED
        order.cancelled_at = now
        order.save(update_fields=("status", "cancelled_at", "updated_at"))
        payment.status = ProviderOrderPaymentOrder.Status.CLOSED
        payment.closed_at = now
        payment.save(update_fields=("status", "closed_at", "updated_at"))
        mark_provider_order_payment_expired(order_no, source="payment_guard")
        return order, payment, False

    acceptance_timeout = operation_rules()["acceptance_timeout_minutes"]
    order.status = ProviderOrder.Status.PENDING_ACCEPTANCE
    order.paid_at = now
    order.acceptance_expires_at = now + timedelta(minutes=acceptance_timeout)
    order.save(update_fields=("status", "paid_at", "acceptance_expires_at", "updated_at"))
    payment.channel = channel
    payment.status = ProviderOrderPaymentOrder.Status.PAID
    payment.gateway_trade_no = gateway_trade_no
    payment.paid_at = now
    payment.save(update_fields=(
        "channel", "status", "gateway_trade_no", "paid_at", "updated_at",
    ))
    cancel_provider_order_payment_expiry(order_no, "payment_succeeded")
    register_provider_acceptance_timeout(order)
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_PAYMENT_SUCCESS,
        title="订单支付成功",
        content="订单已进入待接单，达人会在接单时限内处理。",
    )
    return order, payment, True


def _discounted_order_components(order: ProviderOrder) -> dict[str, int]:
    remaining_discount = order.discount_amount
    service_amount = max(order.service_fee_amount - remaining_discount, 0)
    remaining_discount = max(remaining_discount - order.service_fee_amount, 0)
    other_amount = max(order.other_fee_amount - remaining_discount, 0)
    remaining_discount = max(remaining_discount - order.other_fee_amount, 0)
    transport_amount = max(order.transport_fee_amount - remaining_discount, 0)
    return {
        "service": service_amount,
        "transport": transport_amount,
        "other": other_amount,
    }


def _refund_allocation(order: ProviderOrder, amount: int) -> dict[str, int]:
    # Failed refunds remain retryable, so their amount must stay reserved to avoid
    # issuing another refund against the same paid balance.
    reserved = order.refund_orders.aggregate(
        service=Sum("service_fee_refund_amount"),
        transport=Sum("transport_fee_refund_amount"),
        other=Sum("other_fee_refund_amount"),
        total=Sum("refund_amount"),
    )
    components = _discounted_order_components(order)
    if amount <= 0:
        raise ValidationError({"approved_amount": "核准退款金额必须大于 0。"})
    if amount > order.payable_amount - (reserved["total"] or 0):
        raise ValidationError({"approved_amount": "核准退款金额超过当前可退金额。"})

    remaining = amount
    allocation = {"service": 0, "transport": 0, "other": 0}
    # Partial refunds preserve the provider's incurred transport fee until the end.
    for key in ("service", "other", "transport"):
        available = max(components[key] - (reserved[key] or 0), 0)
        allocated = min(remaining, available)
        allocation[key] = allocated
        remaining -= allocated
    if remaining:
        raise ValidationError({"approved_amount": "退款金额无法按订单费用构成分配。"})
    return allocation


@transaction.atomic
def create_provider_order_refund(
    *,
    order_no: str,
    amount: int,
    source_type: str,
    source_reference: str,
    idempotency_key: str,
    reason: str,
    operator=None,
):
    from taskcenter.services import cancel_provider_order_settlement

    order = ProviderOrder.objects.select_for_update().select_related(
        "customer", "provider", "service__category"
    ).get(order_no=order_no)
    existing = ProviderOrderRefundOrder.objects.filter(
        idempotency_key=idempotency_key
    ).first()
    if existing:
        if existing.order_id != order.id or existing.refund_amount != amount:
            raise ValidationError("退款幂等键对应的业务参数不一致。")
        return existing, False
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if not payment or payment.status not in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
    ):
        raise ValidationError("订单缺少可退款支付单。")
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement and settlement.status == ProviderOrderSettlement.Status.SETTLED:
        raise ValidationError("订单资金已经结算，不能直接退款，请转异常交易处理。")
    allocation = _refund_allocation(order, amount)
    refund = ProviderOrderRefundOrder.objects.create(
        idempotency_key=idempotency_key,
        order=order,
        payment_order=payment,
        beneficiary=order.customer,
        source_type=source_type,
        source_reference=source_reference,
        service_fee_refund_amount=allocation["service"],
        transport_fee_refund_amount=allocation["transport"],
        other_fee_refund_amount=allocation["other"],
        refund_amount=amount,
        allocation_snapshot={
            "version": "provider-refund-allocation-v1",
            "priority": ["service", "other", "transport"],
            "service_fee_refund_amount": allocation["service"],
            "transport_fee_refund_amount": allocation["transport"],
            "other_fee_refund_amount": allocation["other"],
        },
        reason=reason,
        operator=operator,
    )
    if settlement and settlement.status == ProviderOrderSettlement.Status.RISK_FROZEN:
        settlement.status = ProviderOrderSettlement.Status.DISPUTE_FROZEN
        settlement.dispute_reason = f"退款处理中：{source_reference}"
        settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
        cancel_provider_order_settlement(order.order_no, "refund_processing")
    return refund, True


def _settlement_amounts(order: ProviderOrder, commission_rate: Decimal) -> dict:
    components = _discounted_order_components(order)
    refunded = order.refund_orders.filter(
        status=ProviderOrderRefundOrder.Status.SUCCEEDED
    ).aggregate(
        service=Sum("service_fee_refund_amount"),
        transport=Sum("transport_fee_refund_amount"),
        other=Sum("other_fee_refund_amount"),
        total=Sum("refund_amount"),
    )
    net_service = max(components["service"] - (refunded["service"] or 0), 0)
    net_transport = max(components["transport"] - (refunded["transport"] or 0), 0)
    net_other = max(components["other"] - (refunded["other"] or 0), 0)
    platform_amount = int(
        (Decimal(net_service) * commission_rate / Decimal("100")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    provider_service_amount = net_service - platform_amount
    provider_amount = provider_service_amount + net_transport + net_other
    refunded_amount = refunded["total"] or 0
    return {
        "paid_amount": order.payable_amount,
        "refunded_amount": refunded_amount,
        "net_service_fee_amount": net_service,
        "net_transport_fee_amount": net_transport,
        "net_other_fee_amount": net_other,
        "platform_commission_rate": commission_rate,
        "platform_commission_amount": platform_amount,
        "provider_service_income_amount": provider_service_amount,
        "provider_settlement_amount": provider_amount,
        "calculation_snapshot": {
            "version": "provider-order-settlement-v1",
            "commission_basis": "net_service_fee",
            "transport_destination": "provider",
            "paid_amount": order.payable_amount,
            "refunded_amount": refunded_amount,
            "commission_rate": str(commission_rate),
        },
    }


def _apply_settlement_amounts(settlement, amounts):
    for field, value in amounts.items():
        setattr(settlement, field, value)


def _settlement_commission_rate(order: ProviderOrder) -> Decimal:
    value = order.pricing_snapshot.get("platform_commission_rate")
    try:
        rate = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        rate = order.service.category.platform_commission_rate
    if rate < 0 or rate > 100:
        return order.service.category.platform_commission_rate
    return rate


@transaction.atomic
def ensure_provider_order_settlement(*, order_no: str, now=None):
    from backoffice.operation_settings import platform_operation_rules
    from taskcenter.services import register_provider_order_settlement

    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update().select_related(
        "provider__user", "service__category"
    ).get(order_no=order_no)
    confirmed_at = order.customer_confirmed_at or order.auto_confirmed_at
    if not confirmed_at:
        raise ValidationError("订单尚未确认完成，不能进入结算。")
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if not payment or payment.status not in (
        ProviderOrderPaymentOrder.Status.PAID,
        ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
        ProviderOrderPaymentOrder.Status.REFUNDED,
    ):
        raise ValidationError("订单缺少已支付的支付单。")
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement:
        return settlement, False
    rate = _settlement_commission_rate(order)
    amounts = _settlement_amounts(order, rate)
    freeze_until = confirmed_at + timedelta(
        days=platform_operation_rules()["provider_order_settlement_freeze_days"]
    )
    settlement = ProviderOrderSettlement.objects.create(
        order=order,
        provider=order.provider,
        frozen_at=confirmed_at,
        freeze_until=freeze_until,
        **amounts,
    )
    if settlement.refunded_amount >= settlement.paid_amount:
        settlement.status = ProviderOrderSettlement.Status.CANCELLED
        settlement.cancelled_at = now
        settlement.save(update_fields=("status", "cancelled_at", "updated_at"))
    else:
        register_provider_order_settlement(settlement)
    return settlement, True


@transaction.atomic
def advance_provider_order_settlement(*, order_no: str, now=None) -> dict:
    now = now or timezone.now()
    settlement = ProviderOrderSettlement.objects.select_for_update().select_related(
        "order__provider__user", "order__service__category"
    ).filter(order__order_no=order_no).first()
    if not settlement:
        return {"state": "missing", "order_no": order_no}
    if settlement.status in (
        ProviderOrderSettlement.Status.SETTLED,
        ProviderOrderSettlement.Status.CANCELLED,
    ):
        return {"state": "not_applicable", "order_no": order_no, "status": settlement.status}
    from backoffice.models import ProviderOrderAfterSalesCase

    if ProviderOrderAfterSalesCase.objects.filter(
        order=settlement.order,
        status__in=(
            ProviderOrderAfterSalesCase.Status.PENDING,
            ProviderOrderAfterSalesCase.Status.PROCESSING,
            ProviderOrderAfterSalesCase.Status.APPROVED,
        ),
    ).exists():
        settlement.status = ProviderOrderSettlement.Status.DISPUTE_FROZEN
        settlement.dispute_reason = "存在待处理订单退款售后"
        settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
        return {"state": "dispute_frozen", "order_no": order_no}
    amounts = _settlement_amounts(
        settlement.order, settlement.platform_commission_rate
    )
    _apply_settlement_amounts(settlement, amounts)
    if settlement.refunded_amount >= settlement.paid_amount:
        settlement.status = ProviderOrderSettlement.Status.CANCELLED
        settlement.cancelled_at = now
        settlement.dispute_reason = ""
        settlement.save()
        return {"state": "cancelled", "order_no": order_no}
    if now < settlement.freeze_until:
        settlement.status = ProviderOrderSettlement.Status.RISK_FROZEN
        settlement.dispute_reason = ""
        settlement.save()
        return {"state": "not_due", "order_no": order_no, "deadline": settlement.freeze_until}
    settlement.status = ProviderOrderSettlement.Status.SETTLED
    settlement.settled_at = now
    settlement.dispute_reason = ""
    settlement.save()
    create_notification(
        recipient=settlement.provider.user,
        category=UserNotification.Category.ORDER,
        event_type=UserNotification.EventType.PROVIDER_ORDER_SETTLED,
        title="订单收入已结算",
        content=(
            f"订单 {order_no} 收入 ¥{settlement.provider_settlement_amount // 100}."
            f"{settlement.provider_settlement_amount % 100:02d} 已结算入账。"
        ),
        target_type="provider_order_settlement",
        target_id=settlement.settlement_no,
        target_title=f"订单结算 {order_no}",
        action_text="查看收入",
        action_url="/pages/income/index",
        dedupe_key=f"provider-order-settlement:{settlement.settlement_no}:settled",
    )
    return {"state": "settled", "order_no": order_no}


def process_provider_order_refund(refund_no: str):
    from .payment_gateway import get_provider_order_payment_gateway
    from taskcenter.services import reopen_provider_order_settlement

    with transaction.atomic():
        refund = ProviderOrderRefundOrder.objects.select_for_update().select_related(
            "payment_order"
        ).get(refund_no=refund_no)
        if refund.status == ProviderOrderRefundOrder.Status.SUCCEEDED:
            return refund, False
        refund.status = ProviderOrderRefundOrder.Status.PROCESSING
        refund.failure_reason = ""
        refund.save(update_fields=("status", "failure_reason", "updated_at"))
        channel = refund.payment_order.channel
        amount = refund.refund_amount

    try:
        result = get_provider_order_payment_gateway(channel).refund(
            refund_no=refund_no, amount=amount
        )
    except Exception as exc:
        ProviderOrderRefundOrder.objects.filter(
            refund_no=refund_no,
            status=ProviderOrderRefundOrder.Status.PROCESSING,
        ).update(
            status=ProviderOrderRefundOrder.Status.FAILED,
            failure_reason=str(exc)[:1000],
            updated_at=timezone.now(),
        )
        raise

    with transaction.atomic():
        refund_ref = ProviderOrderRefundOrder.objects.only("order_id").get(refund_no=refund_no)
        order = ProviderOrder.objects.select_for_update().select_related(
            "customer", "provider__user", "service__category"
        ).get(pk=refund_ref.order_id)
        payment = ProviderOrderPaymentOrder.objects.select_for_update().get(order=order)
        refund = ProviderOrderRefundOrder.objects.select_for_update().get(refund_no=refund_no)
        if refund.status == ProviderOrderRefundOrder.Status.SUCCEEDED:
            return refund, False
        now = timezone.now()
        refund.status = ProviderOrderRefundOrder.Status.SUCCEEDED
        refund.gateway_refund_no = result.gateway_refund_no
        refund.refunded_at = now
        refund.failure_reason = ""
        refund.save(update_fields=(
            "status", "gateway_refund_no", "refunded_at", "failure_reason", "updated_at",
        ))
        refunded_total = order.refund_orders.filter(
            status=ProviderOrderRefundOrder.Status.SUCCEEDED
        ).aggregate(total=Sum("refund_amount"))["total"] or 0
        payment.status = (
            ProviderOrderPaymentOrder.Status.REFUNDED
            if refunded_total >= payment.payable_amount
            else ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED
        )
        payment.save(update_fields=("status", "updated_at"))

        from backoffice.models import ProviderOrderAfterSalesCase

        case = ProviderOrderAfterSalesCase.objects.select_for_update().filter(
            case_no=refund.source_reference,
            order=order,
        ).first()
        if case:
            case.status = ProviderOrderAfterSalesCase.Status.REFUNDED
            case.save(update_fields=("status", "updated_at"))
        if payment.status == ProviderOrderPaymentOrder.Status.REFUNDED:
            order.status = ProviderOrder.Status.REFUNDED
        elif case and order.status == ProviderOrder.Status.AFTER_SALES:
            order.status = case.original_order_status
        order.save(update_fields=("status", "updated_at"))

        settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
        if settlement:
            amounts = _settlement_amounts(order, settlement.platform_commission_rate)
            _apply_settlement_amounts(settlement, amounts)
            if settlement.refunded_amount >= settlement.paid_amount:
                settlement.status = ProviderOrderSettlement.Status.CANCELLED
                settlement.cancelled_at = now
                settlement.dispute_reason = ""
                settlement.save()
            else:
                settlement.status = ProviderOrderSettlement.Status.RISK_FROZEN
                settlement.dispute_reason = ""
                settlement.save()
                reopen_provider_order_settlement(settlement)
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_REFUND_COMPLETED,
            title="订单退款成功",
            content=(
                f"退款 ¥{refund.refund_amount // 100}."
                f"{refund.refund_amount % 100:02d} 已按原支付路径退回。"
            ),
            dedupe_suffix=refund.refund_no,
        )
        return refund, True


def refresh_provider_review_metrics(provider) -> None:
    aggregate = provider.order_reviews.filter(is_visible=True).aggregate(rating=Avg("rating"))
    provider.rating = aggregate["rating"] or Decimal("0.00")
    provider.service_count = ProviderOrder.objects.filter(
        provider=provider,
        status=ProviderOrder.Status.COMPLETED,
    ).count()
    provider.save(update_fields=("rating", "service_count", "updated_at"))


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
            "platform_commission_rate": str(service.category.platform_commission_rate),
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


@transaction.atomic
def expire_provider_order_payment(order_no: str, *, now=None) -> dict:
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update()
        .filter(order_no=order_no)
        .only("order_no", "status", "payment_expires_at", "cancelled_at")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if order.status != ProviderOrder.Status.PENDING_PAYMENT:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if order.payment_expires_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.payment_expires_at,
        }
    order.status = ProviderOrder.Status.CANCELLED
    order.cancelled_at = now
    order.save(update_fields=("status", "cancelled_at", "updated_at"))
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if payment and payment.status == ProviderOrderPaymentOrder.Status.PENDING_PAYMENT:
        payment.status = ProviderOrderPaymentOrder.Status.CLOSED
        payment.closed_at = now
        payment.save(update_fields=("status", "closed_at", "updated_at"))
    return {"state": "expired", "order_no": order_no, "action": "cancelled"}


@transaction.atomic
def expire_provider_acceptance(order_no: str, *, now=None) -> dict:
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update()
        .filter(order_no=order_no)
        .select_related("customer")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if order.status != ProviderOrder.Status.PENDING_ACCEPTANCE:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if not order.acceptance_expires_at:
        return {"state": "invalid", "order_no": order_no, "reason": "missing_deadline"}
    if order.acceptance_expires_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.acceptance_expires_at,
        }
    order.status = ProviderOrder.Status.PENDING_SUPPORT
    order.save(update_fields=("status", "updated_at"))
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
        title="订单已转客服处理",
        content="达人未在时限内接单，平台客服将继续协助处理。",
    )
    return {
        "state": "expired",
        "order_no": order_no,
        "action": "moved_to_support",
    }


@transaction.atomic
def auto_confirm_provider_order(order_no: str, *, now=None) -> dict:
    now = now or timezone.now()
    order = (
        ProviderOrder.objects.select_for_update()
        .filter(order_no=order_no)
        .select_related("customer")
        .first()
    )
    if not order:
        return {"state": "missing", "order_no": order_no}
    if order.status != ProviderOrder.Status.PENDING_CONFIRMATION:
        return {
            "state": "not_applicable",
            "order_no": order_no,
            "order_status": order.status,
        }
    if not order.confirmation_expires_at:
        return {"state": "invalid", "order_no": order_no, "reason": "missing_deadline"}
    if order.confirmation_expires_at > now:
        return {
            "state": "not_due",
            "order_no": order_no,
            "deadline": order.confirmation_expires_at,
        }
    order.status = ProviderOrder.Status.PENDING_REVIEW
    order.auto_confirmed_at = now
    order.save(update_fields=("status", "auto_confirmed_at", "updated_at"))
    ensure_provider_order_settlement(order_no=order.order_no, now=now)
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_AUTO_CONFIRMED,
        title="订单已自动确认完成",
        content="订单已按规则自动确认完成，可以前往订单详情评价本次服务。",
    )
    return {
        "state": "expired",
        "order_no": order_no,
        "action": "auto_confirmed",
    }


def expire_pending_orders(queryset=None) -> int:
    from taskcenter.services import (
        mark_provider_order_payment_expired,
        register_provider_order_payment_expiry,
    )

    queryset = queryset if queryset is not None else ProviderOrder.objects.all()
    now = timezone.now()
    due_orders = list(queryset.filter(
        status=ProviderOrder.Status.PENDING_PAYMENT,
        payment_expires_at__lte=now,
    ).only("order_no", "payment_expires_at"))
    expired = 0
    for order in due_orders:
        register_provider_order_payment_expiry(order)
        outcome = expire_provider_order_payment(order.order_no, now=now)
        if outcome["state"] == "expired":
            expired += 1
            mark_provider_order_payment_expired(order.order_no, source="request_guard")
    return expired
