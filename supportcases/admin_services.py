from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.access import client_ip
from backoffice.models import AdminAuditLog
from notifications.models import UserNotification
from notifications.services import create_notification

from .models import SupportCase, SupportCaseRecord
from .services import support_case_queryset


def admin_support_case_queryset(access):
    queryset = support_case_queryset()
    if access.all_data:
        return queryset
    if not access.city_codes:
        return queryset.none()
    return queryset.filter(city_code__in=access.city_codes)


def _audit(*, case, actor, access, request, action, before, after):
    AdminAuditLog.objects.create(
        actor=actor,
        organization=access.member.organization if access.member else None,
        action=action,
        target_type="support_case",
        target_id=case.case_no,
        before=before,
        after=after,
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )


def _notify_case(*, case, record, event_type, title, content):
    create_notification(
        recipient=case.reporter,
        category=UserNotification.Category.SUPPORT,
        event_type=event_type,
        title=title,
        content=content,
        target_type="support_case",
        target_id=case.case_no,
        target_title=f"工单 {case.case_no} · {case.get_reason_display()}",
        action_text="查看工单详情",
        action_url=f"/pages/support/index?caseNo={case.case_no}",
        dedupe_key=f"support:{case.case_no}:{event_type}:{record.pk}",
    )


@transaction.atomic
def review_support_case(*, case_no, action, result_note, actor, access, request):
    case = get_object_or_404(
        admin_support_case_queryset(access).select_for_update(of=("self",)),
        case_no=case_no,
    )
    transitions = {
        "start_review": (
            (
                SupportCase.Status.PENDING,
                SupportCase.Status.REVIEWING,
            ),
            SupportCase.Status.PROCESSING,
        ),
        "resolve": (
            (
                SupportCase.Status.PENDING,
                SupportCase.Status.PROCESSING,
                SupportCase.Status.REVIEWING,
            ),
            SupportCase.Status.RESOLVED,
        ),
        "reject": (
            (
                SupportCase.Status.PENDING,
                SupportCase.Status.PROCESSING,
                SupportCase.Status.REVIEWING,
            ),
            SupportCase.Status.REJECTED,
        ),
        "close": (
            (SupportCase.Status.RESOLVED, SupportCase.Status.REJECTED),
            SupportCase.Status.CLOSED,
        ),
    }
    allowed, target = transitions[action]
    if case.status not in allowed:
        raise ValidationError("当前工单状态不能执行该操作。")
    previous = case.status
    before = {
        "status": previous,
        "assignee_id": case.assignee_id,
        "result_note": case.result_note,
    }
    case.status = target
    case.assignee = actor
    if action in ("resolve", "reject", "close"):
        case.result_note = result_note
        case.resolved_at = timezone.now()
    else:
        case.resolved_at = None
    case.save(
        update_fields=(
            "status", "assignee", "result_note", "resolved_at", "updated_at",
        )
    )
    record = SupportCaseRecord.objects.create(
        case=case,
        actor=actor,
        record_type=SupportCaseRecord.RecordType.STATUS_CHANGED,
        content=result_note,
        from_status=previous,
        to_status=target,
    )
    _audit(
        case=case,
        actor=actor,
        access=access,
        request=request,
        action=f"support.case.{action}",
        before=before,
        after={
            "status": case.status,
            "assignee_id": case.assignee_id,
            "result_note": case.result_note,
        },
    )
    if action in ("resolve", "reject"):
        is_review_result = case.review_requested_at is not None
        _notify_case(
            case=case,
            record=record,
            event_type=(
                UserNotification.EventType.SUPPORT_REVIEW_RESULT
                if is_review_result
                else UserNotification.EventType.SUPPORT_RESULT
            ),
            title=(
                "工单复核已完成"
                if is_review_result
                else "工单处理已完成"
                if action == "resolve"
                else "工单处理结果已更新"
            ),
            content=result_note,
        )
    return admin_support_case_queryset(access).get(pk=case.pk)


@transaction.atomic
def add_operator_reply(*, case_no, content, actor, access, request):
    case = get_object_or_404(
        admin_support_case_queryset(access).select_for_update(of=("self",)),
        case_no=case_no,
    )
    if case.status not in (
        SupportCase.Status.PENDING,
        SupportCase.Status.PROCESSING,
        SupportCase.Status.REVIEWING,
    ):
        raise ValidationError("该工单已完结，不能继续回复。")
    previous = case.status
    if case.status in (SupportCase.Status.PENDING, SupportCase.Status.REVIEWING):
        case.status = SupportCase.Status.PROCESSING
    case.assignee = actor
    case.save(update_fields=("status", "assignee", "updated_at"))
    record = SupportCaseRecord.objects.create(
        case=case,
        actor=actor,
        record_type=SupportCaseRecord.RecordType.OPERATOR_REPLY,
        content=content,
        from_status=previous if previous != case.status else "",
        to_status=case.status if previous != case.status else "",
    )
    _audit(
        case=case,
        actor=actor,
        access=access,
        request=request,
        action="support.case.reply",
        before={"status": previous},
        after={"status": case.status, "content": content},
    )
    _notify_case(
        case=case,
        record=record,
        event_type=UserNotification.EventType.SUPPORT_REPLY,
        title="客服回复了你的工单",
        content=content,
    )
    return admin_support_case_queryset(access).get(pk=case.pk)
