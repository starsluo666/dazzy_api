"""Local fulfillment gates. No channel calls; callers hold the order row lock.

Only new start/completion actions are assessed. Historical orders are not backfilled.
Evidence and reviews are append-only snapshots, never replacements of order status.
"""
from datetime import timedelta
from math import ceil

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.operation_settings import platform_operation_rules
from .models import ProviderOrder


def snapshot_policy(order):
    if not order.fulfillment_policy:
        rules = platform_operation_rules()
        order.fulfillment_policy = {
            "version": 1,
            "early_minutes": rules["provider_order_early_tolerance_minutes"],
            "late_minutes": rules["provider_order_late_tolerance_minutes"],
        }


def pause_confirmation(order, *, now):
    if order.confirmation_expires_at:
        order.confirmation_remaining_seconds = max(
            0, ceil((order.confirmation_expires_at - now).total_seconds())
        )
        order.confirmation_expires_at = None


def suspend_fulfillment_tasks(order):
    # Invalidate even an already claimed task. Its handler checks this same order
    # lock before mutation; its eventual finish cannot overwrite a reopened task.
    from taskcenter.models import ScheduledTask
    ScheduledTask.objects.filter(
        business_type="provider_order", business_key=order.order_no,
        task_type__in=(ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
                       ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT),
        status__in=(ScheduledTask.Status.PENDING, ScheduledTask.Status.RUNNING),
    ).update(status=ScheduledTask.Status.CANCELLED, finished_at=timezone.now(),
             result={"reason": "fulfillment_review_required"})


def assess_timing(order, *, stage, now):
    snapshot_policy(order)
    policy = order.fulfillment_policy
    expected = order.starts_at if stage == "start" else order.ends_at
    delta_seconds = (now - expected).total_seconds()
    issues = []
    label = "开始服务" if stage == "start" else "提交完成"
    if delta_seconds < -policy["early_minutes"] * 60:
        issues.append((f"{stage}_early", f"{label}过早"))
    elif delta_seconds > policy["late_minutes"] * 60:
        issues.append((f"{stage}_late", f"{label}过晚"))
    if (stage == "completion" and order.billing_type_snapshot == "hourly"
            and order.service_started_at
            and (now - order.service_started_at).total_seconds()
            < (order.duration_minutes - policy["early_minutes"]) * 60):
        issues.append(("duration_short", "实际服务时长不足"))
    if issues:
        order.fulfillment_revision += 1
        order.fulfillment_issues = [*order.fulfillment_issues, *[
            {"code": code, "label": title, "revision": order.fulfillment_revision,
             "recorded_at": now.isoformat(), "expected_at": expected.isoformat(),
             "service_started_at": order.service_started_at.isoformat() if order.service_started_at else None,
             "duration_minutes": order.duration_minutes, "delta_seconds": round(delta_seconds),
             "resolved_at": None}
            for code, title in issues
        ]]
        order.fulfillment_review_required = True
    if order.fulfillment_review_required:
        pause_confirmation(order, now=now)
        suspend_fulfillment_tasks(order)


def resolve_fulfillment_review(order, *, revision, reason, actor, now=None):
    """Called by the scoped, permissioned admin endpoint in an order transaction."""
    from taskcenter.services import reopen_provider_order_confirmation_timeout, reopen_provider_order_settlement
    from .settlement_plans import sync_provider_settlement_plan
    now = now or timezone.now()
    if order.departure_timed_out_at or order.status in (ProviderOrder.Status.CANCELLED, ProviderOrder.Status.REFUNDED):
        raise ValidationError("订单已取消或退款，不能恢复履约；扣分异议请通过申诉撤销处理。")
    if order.refund_orders.exists():
        raise ValidationError("订单已进入退款流程，请先核实退款结果，不能恢复自动确认及分账。")
    if revision != order.fulfillment_revision:
        raise ValidationError({"revision": "订单出现了新的履约记录，请刷新后重新审核。"})
    if not order.fulfillment_review_required:
        return False  # Duplicate review does not move deadlines or issue money.
    if len(reason.strip()) < 2:
        raise ValidationError({"reason": "请填写核实经过及恢复正常的依据。"})
    order.fulfillment_issues = [
        {**issue, "resolved_at": issue.get("resolved_at") or now.isoformat()}
        for issue in order.fulfillment_issues
    ]
    order.fulfillment_reviews = [*order.fulfillment_reviews, {
        "revision": revision, "reason": reason.strip(), "reviewed_at": now.isoformat(),
        "reviewer_id": actor.pk, "reviewer_name": actor.nickname,
    }]
    order.fulfillment_review_required = False
    if (order.status in (ProviderOrder.Status.PENDING_CONFIRMATION, ProviderOrder.Status.AFTER_SALES)
            and order.completion_submitted_at
            and not (order.customer_confirmed_at or order.auto_confirmed_at)):
        # Review delay must not consume the user's remaining confirmation window.
        seconds = order.confirmation_remaining_seconds
        if seconds is None:
            seconds = platform_operation_rules()["provider_order_confirmation_timeout_days"] * 86400
        order.confirmation_expires_at = now + timedelta(seconds=seconds)
    order.confirmation_remaining_seconds = None
    order.save()
    if order.status == ProviderOrder.Status.PENDING_CONFIRMATION:
        reopen_provider_order_confirmation_timeout(order)
    settlement = getattr(order, "settlement", None)
    if settlement:
        sync_provider_settlement_plan(order_no=order.order_no, now=now)
        if settlement.status in (settlement.Status.RISK_FROZEN, settlement.Status.DISPUTE_FROZEN):
            reopen_provider_order_settlement(settlement)
    return True
