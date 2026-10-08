"""Early service termination: local adjudication, never a channel-success shortcut.

The order row serializes requests, decisions, refunds and distribution. Existing
refund transport/quotas/retries remain authoritative. Retained funds stay frozen
for reconciliation until the partial-refund distribution contract is verified.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.models import AdminAuditLog, ProviderOrderAfterSalesCase as Case
from backoffice.refund_authorization import authorize_approval
from notifications.models import UserNotification
from notifications.services import create_order_notification, create_notification
from .models import ProviderOrder, ProviderOrderRefundOrder, ProviderOrderSettlement


OPEN_STATUSES = (Case.Status.PENDING, Case.Status.PROCESSING, Case.Status.APPROVED)
RESPONSIBILITIES = {"provider": "达人责任", "customer": "用户责任", "both": "双方责任", "neither": "非双方责任"}


def notify_provider(case, *, decided=False):
    order = case.order
    create_notification(
        recipient=order.provider.user, category=UserNotification.Category.ORDER,
        event_type=UserNotification.EventType.ORDER_AFTER_SALES_RESULT if decided else UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
        title="提前终止申请已处理" if decided else "订单申请提前终止",
        content="请查看订单中的客服结论及资金进度。" if decided else "本单已进入客服核查，暂停自动确认与分账，请配合核实实际服务情况。",
        target_type="provider_order", target_id=order.order_no, action_text="查看订单",
        action_url=f"/pages/orders/detail?order_no={order.order_no}",
        dedupe_key=f"termination:{case.case_no}:{'result' if decided else 'request'}:provider",
    )


def refundable_components(order):
    from .services import _discounted_order_components
    components = _discounted_order_components(order)
    for refund in order.refund_orders.all():
        for key in components:
            components[key] -= getattr(refund, f"{key}_fee_refund_amount")
    return components


def validate_end(order, ended_at, *, now):
    if not order.service_started_at or not order.service_started_at <= ended_at <= now:
        raise ValidationError({"ended_at": "实际结束时间必须在开始服务之后、当前时间之前。"})


def original_commission_rate(order, *, refund_amount):
    # Do not substitute today's category/tier rate for an old paid order.
    try:
        rate = Decimal(str(order.pricing_snapshot.get("platform_commission_rate")))
        if not rate.is_finite() or not 0 <= rate <= 100:
            raise InvalidOperation
        return rate
    except InvalidOperation:
        if sum(refundable_components(order).values()) == refund_amount:
            return Decimal("0")  # No retained service income to split.
        raise ValidationError("订单缺少有效的原始分成比例快照，请先由财务核账；不能使用当前比例裁定剩余收入。") from None


@transaction.atomic
def request_termination(*, order_no, actor, role, ended_at, reason, evidence_object_keys):
    from .distributions import assert_refund_not_distributed
    from .services import create_customer_provider_order_after_sales_case

    scope = {"customer": actor} if role == "customer" else {"provider__user": actor}
    order = get_object_or_404(ProviderOrder.objects.select_for_update(), order_no=order_no, **scope)
    existing = order.after_sales_cases.filter(status__in=OPEN_STATUSES).first()
    if existing:
        if existing.case_type == Case.CaseType.EARLY_TERMINATION:
            return existing, False
        raise ValidationError("订单已有待处理售后，请联系客服在原工单中处理。")
    if order.status not in (ProviderOrder.Status.IN_SERVICE, ProviderOrder.Status.PENDING_CONFIRMATION):
        raise ValidationError("仅已开始服务、尚未确认完成的订单可以申请提前终止。")
    validate_end(order, ended_at, now=timezone.now())
    assert_refund_not_distributed(order)
    if order.refund_orders.exclude(status=ProviderOrderRefundOrder.Status.SUCCEEDED).exists():
        raise ValidationError("订单已有未完成退款，请联系客服核查原退款单。")
    components = refundable_components(order)
    case, created = create_customer_provider_order_after_sales_case(
        order_no=order_no, customer=order.customer,
        case_type=Case.CaseType.EARLY_TERMINATION, requested_amount=sum(components.values()),
        reason=reason, evidence_object_keys=evidence_object_keys,
    )
    case.creator = actor
    case.termination_snapshot = {
        "version": 1, "requested_by": role, "reported_ended_at": ended_at.isoformat(),
        "service_started_at": order.service_started_at.isoformat(),
        "scheduled_ends_at": order.ends_at.isoformat(),
        "refundable_components": components,
    }
    case.save(update_fields=("creator", "termination_snapshot", "updated_at"))
    # Keep the unused confirmation window if the claim is later rejected.
    if case.original_order_status == ProviderOrder.Status.PENDING_CONFIRMATION:
        from .fulfillment import pause_confirmation
        pause_confirmation(order, now=timezone.now())
        order.save(update_fields=("confirmation_expires_at", "confirmation_remaining_seconds", "updated_at"))
    notify_provider(case)
    return case, created


@transaction.atomic
def resolve_termination(*, case_no, ended_at, responsibility, component_refunds,
                        result_note, actor, access, request):
    from backoffice.access import client_ip
    from .distributions import assert_refund_not_distributed
    from .services import _refund_allocation, create_provider_order_refund
    from .settlement_plans import sync_provider_settlement_plan

    access.require("order.after_sales.review")
    ref = get_object_or_404(Case, case_no=case_no)
    orders = ProviderOrder.objects.select_for_update()
    if not access.all_data:
        orders = orders.filter(provider__service_city_code__in=access.city_codes)
    order = get_object_or_404(orders, pk=ref.order_id)
    case = Case.objects.select_for_update().get(pk=ref.pk)
    if case.case_type != Case.CaseType.EARLY_TERMINATION:
        raise ValidationError("该工单不是提前终止申请。")
    if case.status not in (Case.Status.PENDING, Case.Status.PROCESSING):
        raise ValidationError("该终止申请已处理，请刷新查看结果，勿重复提交。")
    if order.status != ProviderOrder.Status.AFTER_SALES:
        raise ValidationError("订单状态已变化，请刷新后核查。")
    validate_end(order, ended_at, now=timezone.now())
    if responsibility not in RESPONSIBILITIES or len(result_note.strip()) < 5:
        raise ValidationError("请选择责任归属并填写至少 5 个字的核查依据。")
    if set(component_refunds) != {"service", "transport", "other"} or any(
        type(value) is not int or value < 0 for value in component_refunds.values()
    ):
        raise ValidationError("退款明细必须为非负整数分。")
    amount = sum(component_refunds.values())
    assert_refund_not_distributed(order)
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement and settlement.status == ProviderOrderSettlement.Status.SETTLED:
        raise ValidationError("订单已结算，请转财务核查。")
    if order.refund_orders.exclude(status=ProviderOrderRefundOrder.Status.SUCCEEDED).exists():
        raise ValidationError("已有未完成退款，请先核实原退款单。")
    if amount > case.requested_amount:
        raise ValidationError("退款金额超过申请时剩余可退金额。")
    if amount:
        _refund_allocation(order, amount, components_override=component_refunds)
    rate = original_commission_rate(order, refund_amount=amount)
    if not authorize_approval(case, amount=amount, refunds=order.refund_orders.all(),
                              actor=actor, access=access, request=request, result_note=result_note):
        return case  # No status change / money movement when escalated.
    now = timezone.now()
    before = {"case_status": case.status, "order_status": order.status}
    case.termination_snapshot = {**case.termination_snapshot, "decision": {
        "ended_at": ended_at.isoformat(), "responsibility": responsibility,
        "responsibility_label": RESPONSIBILITIES[responsibility],
        "component_refunds": component_refunds,
        "platform_commission_rate": str(rate),
        "credit_handling": "independent_review",  # Never silently penalize a refund.
    }}
    case.status = Case.Status.APPROVED if amount else Case.Status.RESOLVED
    case.approved_amount = amount
    case.result_note = result_note.strip()
    case.reviewed_by, case.reviewed_at = actor, now
    case.save()
    order.status = ProviderOrder.Status.TERMINATED
    order.confirmation_expires_at = None
    order.save(update_fields=("status", "confirmation_expires_at", "updated_at"))
    from .fulfillment import suspend_fulfillment_tasks
    suspend_fulfillment_tasks(order)
    if amount:
        create_provider_order_refund(
            order_no=order.order_no, amount=amount,
            source_type=ProviderOrderRefundOrder.SourceType.AFTER_SALES,
            source_reference=case.case_no, idempotency_key=f"provider-after-sales:{case.case_no}",
            reason=case.result_note, operator=actor, components_override=component_refunds,
        )
    else:
        reconcile_termination_settlement(order)
    sync_provider_settlement_plan(order_no=order.order_no)
    AdminAuditLog.objects.create(
        actor=actor, organization=access.member.organization if access.member else None,
        action="order.after_sales.resolve_termination", target_type="provider_order_after_sales_case",
        target_id=case.case_no, before=before,
        after={"case_status": case.status, "order_status": order.status,
               "approved_amount": amount, "decision": case.termination_snapshot["decision"],
               "result_note": case.result_note},
        request_id=request.headers.get("X-Request-ID", ""), ip_address=client_ip(request),
    )
    create_order_notification(
        order=order, event_type=UserNotification.EventType.ORDER_AFTER_SALES_RESULT,
        title="服务已提前终止", content=f"{case.result_note} 退款进度请查看订单详情。",
        dedupe_suffix=f"{case.case_no}:termination",
    )
    notify_provider(case, decided=True)
    return case


def reconcile_termination_settlement(order):
    """Caller holds the order lock. Only verified refunds affect net entitlement."""
    from .services import _settlement_amounts
    from .settlement_plans import sync_provider_settlement_plan
    if order.refund_orders.exclude(status=ProviderOrderRefundOrder.Status.SUCCEEDED).exists():
        return
    case = order.after_sales_cases.filter(case_type=Case.CaseType.EARLY_TERMINATION,
                                          status__in=(Case.Status.APPROVED, Case.Status.REFUNDED, Case.Status.RESOLVED)).first()
    if not case:
        return
    # Never roll back a verified refund because an accounting snapshot is missing.
    # Such cases stay visible in the reconciliation queue for manual investigation.
    try:
        rate = Decimal(case.termination_snapshot.get("decision", {}).get("platform_commission_rate", ""))
        if not rate.is_finite() or not 0 <= rate <= 100:
            return
    except (InvalidOperation, TypeError):
        return
    amounts = _settlement_amounts(order, rate)
    now = timezone.now()
    cancelled = amounts["refunded_amount"] >= amounts["paid_amount"]
    ProviderOrderSettlement.objects.update_or_create(order=order, defaults={
        **amounts, "provider": order.provider, "frozen_at": now, "freeze_until": now,
        "status": ProviderOrderSettlement.Status.CANCELLED if cancelled else ProviderOrderSettlement.Status.DISPUTE_FROZEN,
        "dispute_reason": "" if cancelled else "提前终止剩余款待核账，尚未开放渠道分账",
        "cancelled_at": now if cancelled else None,
    })
    sync_provider_settlement_plan(order_no=order.order_no)  # Manual review; never a payout job.


def termination_payload(case):
    if not case or case.case_type != Case.CaseType.EARLY_TERMINATION:
        return None
    data = dict(case.termination_snapshot)
    if "decision" in data:
        data["decision"] = {key: value for key, value in data["decision"].items() if key != "platform_commission_rate"}
    decision = data.get("decision")
    if not decision:
        state, label = ("rejected", "申请已驳回") if case.status == Case.Status.REJECTED else ("review_pending", "待客服核定")
    elif case.status == Case.Status.APPROVED:
        state, label = "refund_pending", "退款处理中，剩余款暂停结算"
    else:
        refunded = sum(r.refund_amount for r in case.order.refund_orders.all() if r.status == "succeeded")
        state, label = ("closed", "已全额退款，无剩余款") if refunded >= case.order.payable_amount else (
            "reconciliation_pending", "剩余款待财务核账，尚未计入可提现余额"
        )
    return {**data, "finance_state": state, "finance_label": label}
