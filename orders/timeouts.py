"""Idle fulfillment watchdogs. No network calls; serialize with actions/refunds on the order.

Only orders explicitly enrolled at creation are eligible. Never backfill deadlines
from current configuration: that would retroactively impose financial penalties.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.models import ProviderCreditAdjustment
from backoffice.operation_settings import platform_operation_rules
from notifications.models import UserNotification
from notifications.services import create_notification
from providers.models import ProviderProfile
from .fulfillment import pause_confirmation, suspend_fulfillment_tasks
from .models import ProviderOrder, ProviderOrderPaymentOrder, ProviderOrderRefundOrder


def enroll_fulfillment_timeouts(order):
    """Call ONLY in the new-order creation transaction, before exposing the order."""
    rules = platform_operation_rules()
    order.fulfillment_policy = {
        "version": 2,
        "early_minutes": rules["provider_order_early_tolerance_minutes"],
        "late_minutes": rules["provider_order_late_tolerance_minutes"],
        "departure_grace_minutes": rules["provider_order_departure_grace_minutes"],
        "no_departure_credit_penalty": rules["provider_order_no_departure_credit_penalty"],
    }
    order.departure_deadline_at = order.starts_at + timedelta(minutes=order.fulfillment_policy["departure_grace_minutes"])
    order.start_deadline_at = order.starts_at + timedelta(minutes=order.fulfillment_policy["late_minutes"])
    order.completion_deadline_at = order.ends_at + timedelta(minutes=order.fulfillment_policy["late_minutes"])
    order.save(update_fields=("fulfillment_policy", "departure_deadline_at", "start_deadline_at", "completion_deadline_at"))


def _notify(order, *, event, title, content, suffix="", provider_only=False):
    recipients = [(order.provider.user, f"/pages/orders/detail?order_no={order.order_no}")]
    if not provider_only:
        recipients.append((order.customer, f"/pages/orders/detail?orderNo={order.order_no}"))
    for recipient, url in recipients:
        create_notification(
            recipient=recipient, category=UserNotification.Category.ORDER, event_type=event,
            title=title, content=content, target_type="provider_order", target_id=order.order_no,
            action_text="查看订单", action_url=url,
            dedupe_key=f"order-watchdog:{order.order_no}:{event}:{suffix}:{recipient.pk}",
        )


def flag_idle_fulfillment(order, *, code, label, expected, now):
    # A reviewed idle event stays reviewed. A later actual start/completion may
    # still generate a NEW timing anomaly through assess_timing.
    if any(issue.get("code") == code for issue in order.fulfillment_issues):
        return
    order.fulfillment_revision += 1
    order.fulfillment_issues = [*order.fulfillment_issues, {
        "code": code, "label": label, "revision": order.fulfillment_revision,
        "recorded_at": now.isoformat(), "expected_at": expected.isoformat(),
        "delta_seconds": round((now - expected).total_seconds()), "resolved_at": None,
    }]
    order.fulfillment_review_required = True
    pause_confirmation(order, now=now)
    suspend_fulfillment_tasks(order)
    order.save()
    _notify(order, event=UserNotification.EventType.ORDER_FULFILLMENT_HELD,
            title="订单履约需客服核实", content=f"{label}。自动确认及分账已暂停，请联系客服。", suffix=code)


def _manual_intervention_reason(order):
    from supportcases.models import SupportCase
    if order.fulfillment_review_required:
        return "已有履约异常待审核"
    if any(issue.get("code") == "departure_timeout_manual" for issue in order.fulfillment_issues):
        return "本次超时已转人工处理"
    if order.support_contacted_at or order.support_notes.exists():
        return "已有客服介入记录"
    if order.after_sales_cases.filter(status__in=("pending", "processing", "approved")).exists():
        return "已有售后申请处理中"
    if SupportCase.objects.filter(provider_order=order, status__in=("pending", "processing", "reviewing")).exists():
        return "已有客服工单待处理"
    if order.refund_orders.exclude(status=ProviderOrderRefundOrder.Status.SUCCEEDED).exists():
        return "已有退款处理中或待核实"
    return ""


def _deduct_credit(order):
    profile = ProviderProfile.objects.select_for_update().get(pk=order.provider_id)
    points = min(profile.credit_score, order.fulfillment_policy["no_departure_credit_penalty"])
    before = profile.credit_score
    profile.credit_score -= points
    profile.save(update_fields=("credit_score", "updated_at"))
    ProviderCreditAdjustment.objects.create(
        provider=profile, order=order, source=ProviderCreditAdjustment.Source.NO_DEPARTURE,
        delta=-points, before_score=before, after_score=profile.credit_score,
        reason=f"订单 {order.order_no} 已接单但超时未出发",
    )
    order.timeout_credit_points = points


@transaction.atomic
def expire_provider_departure(order_no, *, now=None):
    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update(of=("self",)).filter(order_no=order_no).first()
    if not order or not order.departure_deadline_at:
        return {"state": "not_enrolled"}
    if order.departure_timed_out_at:
        return {"state": "already_expired"}
    if order.status != ProviderOrder.Status.PENDING_SERVICE or not order.accepted_at:
        return {"state": "not_applicable"}
    if now < order.departure_deadline_at:
        return {"state": "not_due", "deadline": order.departure_deadline_at}
    reason = _manual_intervention_reason(order)
    if order.departed_at or order.arrival_photo_id or order.arrival_photo_uploaded_at or order.service_started_at or order.completion_submitted_at:
        reason = "订单已有履约凭证，需核实是否实际服务"
    payment = ProviderOrderPaymentOrder.objects.select_for_update().filter(order=order).first()
    if (not payment or not order.paid_at or not payment.paid_at
            or payment.status not in ("paid", "partially_refunded")
            or payment.payable_amount != order.payable_amount or payment.payer_id != order.customer_id):
        reason = "支付状态异常，需核实实收金额"
    if reason:
        flag_idle_fulfillment(order, code="departure_timeout_manual", label=f"未出发超时：{reason}",
                              expected=order.departure_deadline_at, now=now)
        return {"state": "manual_review"}

    from .services import create_provider_order_refund
    from .distributions import assert_refund_not_distributed
    from .coupons import release_coupon
    reference = f"provider-no-departure:{order.order_no}"
    refunded = order.refund_orders.aggregate(total=Sum("refund_amount"))["total"] or 0
    remaining = order.payable_amount - refunded
    try:
        # Savepoint keeps any failed financial validation from partially reserving
        # money. Do not suppress DB/transport errors; the worker must retry those.
        with transaction.atomic():
            assert_refund_not_distributed(order)
            settlement = getattr(order, "settlement", None)
            if settlement and settlement.status == "settled":
                raise ValidationError("资金已结算，不能自动退款")
            if remaining < 0 or (remaining == 0 and order.payable_amount != 0):
                raise ValidationError("退款金额需人工核实")
            if remaining:
                create_provider_order_refund(
                    order_no=order.order_no, amount=remaining,
                    source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
                    source_reference=reference, idempotency_key=reference,
                    reason="达人已接单但超过出发截止时间仍未出发，退还剩余实付金额（含路费）。",
                )
    except ValidationError:
        flag_idle_fulfillment(order, code="departure_timeout_manual", label="未出发超时：退款或资金状态需人工核实",
                              expected=order.departure_deadline_at, now=now)
        return {"state": "manual_review"}
    order.departure_timed_out_at = now
    order.cancelled_at = now
    order.status = ProviderOrder.Status.CANCELLED
    _deduct_credit(order)
    if not remaining:
        payment.status = ProviderOrderPaymentOrder.Status.REFUNDED
        payment.save(update_fields=("status", "updated_at"))
        order.status = ProviderOrder.Status.REFUNDED
        release_coupon(order, refunded=True)
    order.save(update_fields=("status", "cancelled_at", "departure_timed_out_at", "timeout_credit_points", "updated_at"))
    suspend_fulfillment_tasks(order)
    _notify(order, event=UserNotification.EventType.ORDER_DEPARTURE_TIMEOUT, title="订单因达人超时未出发而取消",
            content="订单已取消，剩余实付金额将按原支付路径退回，到账以退款结果为准。" if remaining else "订单已关闭，无实付款需退回。")
    _notify(order, event=UserNotification.EventType.PROVIDER_CREDIT_CHANGED, title="超时未出发信用分调整",
            content=f"本单扣减信用分 {order.timeout_credit_points} 分。如有异议，请携订单号联系客服申诉。", provider_only=True)
    return {"state": "expired", "refund_amount": remaining, "credit_points": order.timeout_credit_points}


@transaction.atomic
def inspect_idle_fulfillment(order_no, *, stage, now=None):
    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update(of=("self",)).filter(order_no=order_no).first()
    if not order or not order.departure_deadline_at:
        return {"state": "not_enrolled"}
    if stage == "reminder":
        expected = order.starts_at - timedelta(minutes=30)
        applicable = order.status == ProviderOrder.Status.PENDING_SERVICE and not order.departed_at
    elif stage == "start":
        expected = order.start_deadline_at
        applicable = order.status == ProviderOrder.Status.DEPARTED and not order.service_started_at
    else:
        expected = order.completion_deadline_at
        applicable = order.status == ProviderOrder.Status.IN_SERVICE and not order.completion_submitted_at
    if not applicable or not expected:
        return {"state": "not_applicable"}
    if now < expected or (stage != "reminder" and now == expected):
        return {"state": "not_due", "deadline": expected if stage == "reminder" else expected + timedelta(seconds=1)}
    if stage == "reminder":
        if now >= order.departure_deadline_at:
            return {"state": "not_applicable"}
        deadline_text = timezone.localtime(order.departure_deadline_at).strftime("%m月%d日 %H:%M")
        _notify(order, event=UserNotification.EventType.ORDER_DEPARTURE_REMINDER, title="请及时联系用户并出发",
                content=f"请按预约时间到场，联系确认后点击出发。{deadline_text} 前仍未出发，订单将按超时规则取消并扣分。",
                provider_only=True)
    else:
        label = "已出发但超时未开始服务" if stage == "start" else "服务已超过结束时间但未提交完成"
        flag_idle_fulfillment(order, code=f"idle_{stage}", label=label, expected=expected, now=now)
    return {"state": "processed"}


def reverse_departure_penalty(order, *, actor, reason, organization=None):
    """Permissioned admin caller holds order lock. Reversal never resurrects money/order."""
    if not order.departure_timed_out_at:
        raise ValidationError("该订单没有未出发超时扣分记录。")
    if order.timeout_credit_reversed_at:
        return False
    profile = ProviderProfile.objects.select_for_update().get(pk=order.provider_id)
    restored = min(order.timeout_credit_points, max(0, 100 - profile.credit_score))
    before = profile.credit_score
    profile.credit_score += restored
    profile.save(update_fields=("credit_score", "updated_at"))
    ProviderCreditAdjustment.objects.create(
        provider=profile, order=order, source=ProviderCreditAdjustment.Source.NO_DEPARTURE_REVERSAL,
        operator=actor, organization=organization, delta=restored, before_score=before,
        after_score=profile.credit_score, reason=reason,
    )
    order.timeout_credit_reversed_at = timezone.now()
    order.save(update_fields=("timeout_credit_reversed_at", "updated_at"))
    _notify(order, event=UserNotification.EventType.PROVIDER_CREDIT_CHANGED, title="超时扣分申诉已通过",
            content=f"已撤销本单扣分，实际恢复 {restored} 分（最高 100 分）。订单取消及退款结果不变。",
            suffix="reversed", provider_only=True)
    return True


def timeout_summary(order):
    """Small shared projection for customer/provider/admin; never assumes refund succeeded."""
    refund_label = ""
    if order.departure_timed_out_at:
        if refund_needs_attention(order):
            refund_label = "退款异常，客服核实中"
        elif order.status == ProviderOrder.Status.REFUNDED:
            refund_label = "已退款" if order.payable_amount else "无需退款"
        else:
            refund_label = "退款处理中"
    return {
        "departure_deadline_at": order.departure_deadline_at,
        "timed_out_at": order.departure_timed_out_at,
        "reason": "达人超时未出发" if order.departure_timed_out_at else "",
        "refund_label": refund_label,
        "credit_points": order.timeout_credit_points,
        "credit_reversed_at": order.timeout_credit_reversed_at,
        "configured_credit_penalty": order.fulfillment_policy.get("no_departure_credit_penalty"),
    }


def refund_attention_query():
    from django.db.models import Q
    from taskcenter.models import ScheduledTask
    failed_tasks = ScheduledTask.objects.filter(task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND,
                                               status=ScheduledTask.Status.FAILED).values("business_key")
    refunds = ProviderOrderRefundOrder.objects.filter(
        Q(status="failed") | (Q(refund_no__in=failed_tasks) & ~Q(status="succeeded"))
    )
    return Q(pk__in=refunds.values("order_id"))


def refund_needs_attention(order):
    from taskcenter.models import ScheduledTask
    refunds = list(order.refund_orders.all())
    if any(item.status == "failed" for item in refunds):
        return True
    pending = [item.refund_no for item in refunds if item.status != "succeeded"]
    return bool(pending) and ScheduledTask.objects.filter(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND, status=ScheduledTask.Status.FAILED,
        business_key__in=pending,
    ).exists()


def legacy_overdue_query(*, now=None):
    from django.db.models import Q
    now = now or timezone.now()
    # A triage filter only, never an instruction to debit/refund old orders.
    return Q(departure_deadline_at__isnull=True) & (
        Q(status__in=("pending_service", "departed"), starts_at__lt=now - timedelta(minutes=30))
        | Q(status="in_service", ends_at__lt=now - timedelta(minutes=30))
    )


def is_legacy_overdue(order):
    if order.departure_deadline_at:
        return False
    expected = order.ends_at if order.status == "in_service" else order.starts_at
    return order.status in ("pending_service", "departed", "in_service") and expected < timezone.now() - timedelta(minutes=30)
