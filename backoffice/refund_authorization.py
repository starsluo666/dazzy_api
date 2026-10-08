"""Staff refund authorization; never calls the gateway or edits payment outcomes.

Approvers are locked until the enclosing business transaction creates its refund.
Both order families share a daily budget. Failed/in-flight refunds still occupy it.
The order limit uses all reserved refunds, not just this operator's current request.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from activities.models import ActivityParticipationRefundOrder
from orders.models import ProviderOrderRefundOrder
from .access import client_ip
from .models import AdminAuditLog
from .operation_settings import platform_operation_rules


def has(access, permission):
    return "*" in access.permissions or permission in access.permissions


def require_approval(access):
    if not (has(access, "refund.approve") or has(access, "refund.supervise")):
        raise PermissionDenied("当前账号只能登记或处理售后，未获退款审批授权。")


def daily_usage(actor, *, now=None):
    now = now or timezone.now()
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    start = datetime.combine(day, time.min, tzinfo=ZoneInfo("Asia/Shanghai"))
    return sum(
        model.objects.filter(
            operator=actor, created_at__gte=start, created_at__lt=start + timedelta(days=1)
        ).aggregate(total=Sum("refund_amount"))["total"]
        or 0
        for model in (ProviderOrderRefundOrder, ActivityParticipationRefundOrder)
    )


def refund_policy(actor, access):
    rules = platform_operation_rules()
    used = daily_usage(actor)
    return {
        "single_limit": rules["support_refund_single_limit"],
        "daily_limit": rules["support_refund_daily_limit"],
        "daily_used": used,
        "daily_remaining": max(0, rules["support_refund_daily_limit"] - used),
        "can_approve": has(access, "refund.approve") or has(access, "refund.supervise"),
        "supervisor": has(access, "refund.supervise"),
        "timezone": "Asia/Shanghai",
    }


def escalate(case, *, reason, actor, access, request, audit_details=None):
    before = {"requires_supervisor": case.requires_supervisor}
    case.requires_supervisor = True
    case.escalation_reason = reason[:200]
    case.save(update_fields=("requires_supervisor", "escalation_reason", "updated_at"))
    AdminAuditLog.objects.create(
        actor=actor,
        organization=access.member.organization if access.member else None,
        action="refund.escalate",
        target_type=case._meta.model_name,
        target_id=case.case_no,
        before=before,
        after={"requires_supervisor": True, "reason": reason, **(audit_details or {})},
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    return case


def authorize_approval(case, *, amount, refunds, actor, access, request, result_note):
    """Called with the order/payment lock in an atomic transaction. False = escalated, NOT approved."""
    require_approval(access)
    User.objects.select_for_update().get(pk=actor.pk)
    if has(access, "refund.supervise"):
        return True
    rules = platform_operation_rules()
    reserved = refunds.aggregate(total=Sum("refund_amount"))["total"] or 0
    reason = ""
    if case.requires_supervisor:
        reason = case.escalation_reason or "本单已转主管审核，普通客服不能批准。"
    elif not rules["support_refund_single_limit"] or not rules["support_refund_daily_limit"]:
        reason = "客服退款额度尚未开放，请由主管审核。"
    elif reserved + amount > rules["support_refund_single_limit"]:
        reason = "本订单累计退款超过客服单订单上限，请由主管审核。"
    elif daily_usage(actor) + amount > rules["support_refund_daily_limit"]:
        reason = "今日两类订单累计审批超过客服日额度，请由主管审核。"
    if reason:
        escalate(
            case,
            reason=reason,
            actor=actor,
            access=access,
            request=request,
            audit_details={
                "proposed_amount": amount,
                "result_note": result_note,
                "single_limit": rules["support_refund_single_limit"],
                "daily_limit": rules["support_refund_daily_limit"],
            },
        )
        return False
    return True


def require_supervisor_for_escalated(case, access):
    if case.requires_supervisor and not has(access, "refund.supervise"):
        raise PermissionDenied("本单已转主管，请由具有主管退款审核权限的人员处理。")
