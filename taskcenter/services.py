from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import CharField, Exists, F, OuterRef, Q, Subquery
from django.db.models.functions import Cast
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ScheduledTask


TASK_LEASE_TIMEOUT = timedelta(minutes=5)
TASK_RETRY_BASE_DELAY = timedelta(minutes=1)
TASK_SYNC_BATCH_SIZE = 500
ACTIVITY_TASK_TYPES = (
    ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY,
    ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY,
    ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
    ScheduledTask.Type.ACTIVITY_FORMATION_DEADLINE,
    ScheduledTask.Type.ACTIVITY_START,
    ScheduledTask.Type.ACTIVITY_COMPLETION,
    ScheduledTask.Type.ACTIVITY_SETTLEMENT,
)


@dataclass(frozen=True)
class TaskExecutionOutcome:
    status: str
    result: dict = field(default_factory=dict)
    available_at: object | None = None


def task_dedupe_key(task_type: str, business_type: str, business_key: str) -> str:
    return f"{task_type}:{business_type}:{business_key}"


def _register_task(*, task_type, business_type, business_key, scheduled_at, payload=None):
    dedupe_key = task_dedupe_key(task_type, business_type, business_key)
    task, created = ScheduledTask.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            "task_type": task_type,
            "business_type": business_type,
            "business_key": business_key,
            "payload": payload or {},
            "scheduled_at": scheduled_at,
            "available_at": scheduled_at,
        },
    )
    if not created and task.status == ScheduledTask.Status.PENDING:
        changed_fields = []
        if task.scheduled_at != scheduled_at:
            task.scheduled_at = scheduled_at
            task.available_at = scheduled_at
            changed_fields.extend(("scheduled_at", "available_at"))
        next_payload = payload or {}
        if task.payload != next_payload:
            task.payload = next_payload
            changed_fields.append("payload")
        if changed_fields:
            task.save(update_fields=(*changed_fields, "updated_at"))
    return task, created


def register_provider_order_payment_expiry(order):
    return _register_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
        business_type="provider_order",
        business_key=order.order_no,
        scheduled_at=order.payment_expires_at,
        payload={"order_no": order.order_no},
    )


def register_provider_acceptance_timeout(order):
    if not order.acceptance_expires_at:
        raise ValueError("达人接单截止时间不能为空。")
    return _register_task(
        task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
        business_type="provider_order",
        business_key=order.order_no,
        scheduled_at=order.acceptance_expires_at,
        payload={"order_no": order.order_no},
    )


def register_provider_order_confirmation_timeout(order):
    if not order.confirmation_expires_at:
        raise ValueError("用户确认截止时间不能为空。")
    return _register_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
        business_type="provider_order",
        business_key=order.order_no,
        scheduled_at=order.confirmation_expires_at,
        payload={"order_no": order.order_no},
    )


def register_provider_order_settlement(settlement):
    return _register_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
        business_type="provider_order",
        business_key=settlement.order.order_no,
        scheduled_at=settlement.freeze_until,
        payload={
            "order_no": settlement.order.order_no,
            "settlement_no": settlement.settlement_no,
        },
    )


def register_provider_order_refund(refund):
    return _register_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND,
        business_type="provider_order_refund",
        business_key=refund.refund_no,
        scheduled_at=timezone.now(),
        payload={
            "refund_no": refund.refund_no,
            "order_no": refund.order.order_no,
        },
    )


def register_activity_publish_payment_expiry(order):
    return _register_task(
        task_type=ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY,
        business_type="activity_publish_payment",
        business_key=order.order_no,
        scheduled_at=order.expires_at,
        payload={
            "publish_order_no": order.order_no,
            "activity_id": order.activity_id,
            "activity_title": order.activity.title,
        },
    )


def register_activity_participation_payment_expiry(order):
    return _register_task(
        task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY,
        business_type="activity_participation",
        business_key=order.order_no,
        scheduled_at=order.expires_at,
        payload={
            "payment_order_no": order.order_no,
            "participation_id": order.participation_id,
            "activity_id": order.participation.activity_id,
            "activity_title": order.participation.activity.title,
        },
    )


def register_activity_participation_refund(refund):
    return _register_task(
        task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
        business_type="activity_participation_refund",
        business_key=refund.refund_no,
        scheduled_at=timezone.now(),
        payload={
            "refund_no": refund.refund_no,
            "payment_order_no": refund.payment_order.order_no,
            "participation_id": refund.participation_id,
            "activity_id": refund.activity_id,
            "activity_title": refund.activity.title,
        },
    )


def _register_activity_task(*, activity, task_type, scheduled_at):
    return _register_task(
        task_type=task_type,
        business_type="activity",
        business_key=str(activity.pk),
        scheduled_at=scheduled_at,
        payload={"activity_id": activity.pk, "activity_title": activity.title},
    )


def register_activity_formation_deadline(activity):
    return _register_activity_task(
        activity=activity,
        task_type=ScheduledTask.Type.ACTIVITY_FORMATION_DEADLINE,
        scheduled_at=activity.formation_deadline,
    )


def register_activity_start(activity):
    return _register_activity_task(
        activity=activity,
        task_type=ScheduledTask.Type.ACTIVITY_START,
        scheduled_at=activity.starts_at,
    )


def register_activity_completion(activity):
    return _register_activity_task(
        activity=activity,
        task_type=ScheduledTask.Type.ACTIVITY_COMPLETION,
        scheduled_at=activity.ends_at,
    )


def register_activity_lifecycle_tasks(activity):
    formation = register_activity_formation_deadline(activity)
    start = register_activity_start(activity)
    completion = register_activity_completion(activity)
    return {"formation": formation, "start": start, "completion": completion}


def activity_settlement_deadline(settlement):
    if settlement.status == settlement.Status.CONFIRMING:
        return settlement.confirmation_deadline
    if settlement.status == settlement.Status.RISK_FROZEN:
        return settlement.freeze_until
    return max(settlement.confirmation_deadline, timezone.now())


def register_activity_settlement(settlement):
    return _register_task(
        task_type=ScheduledTask.Type.ACTIVITY_SETTLEMENT,
        business_type="activity",
        business_key=str(settlement.activity_id),
        scheduled_at=activity_settlement_deadline(settlement),
        payload={
            "activity_id": settlement.activity_id,
            "activity_title": settlement.activity.title,
            "settlement_no": settlement.settlement_no,
        },
    )


def cancel_business_task(*, task_type, business_type, business_key, reason):
    now = timezone.now()
    return ScheduledTask.objects.filter(
        dedupe_key=task_dedupe_key(task_type, business_type, business_key),
        status=ScheduledTask.Status.PENDING,
    ).update(
        status=ScheduledTask.Status.CANCELLED,
        finished_at=now,
        result={"reason": reason},
        updated_at=now,
    )


def cancel_provider_order_payment_expiry(order_no: str, reason: str):
    return cancel_business_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
        business_type="provider_order",
        business_key=order_no,
        reason=reason,
    )


def cancel_provider_acceptance_timeout(order_no: str, reason: str):
    return cancel_business_task(
        task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
        business_type="provider_order",
        business_key=order_no,
        reason=reason,
    )


def cancel_provider_order_confirmation_timeout(order_no: str, reason: str):
    return cancel_business_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
        business_type="provider_order",
        business_key=order_no,
        reason=reason,
    )


def cancel_provider_order_settlement(order_no: str, reason: str):
    return cancel_business_task(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
        business_type="provider_order",
        business_key=order_no,
        reason=reason,
    )


def cancel_activity_participation_payment_expiry(order_no: str, reason: str):
    return cancel_business_task(
        task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY,
        business_type="activity_participation",
        business_key=order_no,
        reason=reason,
    )


def cancel_activity_publish_payment_expiry(order_no: str, reason: str):
    return cancel_business_task(
        task_type=ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY,
        business_type="activity_publish_payment",
        business_key=order_no,
        reason=reason,
    )


def reopen_provider_order_settlement(settlement):
    scheduled_at = max(settlement.freeze_until, timezone.now())
    dedupe_key = task_dedupe_key(
        ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
        "provider_order",
        settlement.order.order_no,
    )
    task = ScheduledTask.objects.select_for_update().filter(dedupe_key=dedupe_key).first()
    if task is None:
        return register_provider_order_settlement(settlement)
    if task.status == ScheduledTask.Status.PENDING:
        return _register_task(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
            business_type="provider_order",
            business_key=settlement.order.order_no,
            scheduled_at=scheduled_at,
            payload={
                "order_no": settlement.order.order_no,
                "settlement_no": settlement.settlement_no,
            },
        )
    if task.status != ScheduledTask.Status.CANCELLED:
        return task, False
    task.status = ScheduledTask.Status.PENDING
    task.scheduled_at = scheduled_at
    task.available_at = scheduled_at
    task.attempt_count = 0
    task.started_at = None
    task.finished_at = None
    task.last_error = ""
    task.result = {}
    task.save(update_fields=(
        "status", "scheduled_at", "available_at", "attempt_count", "started_at",
        "finished_at", "last_error", "result", "updated_at",
    ))
    return task, False


def reopen_provider_order_confirmation_timeout(order):
    if not order.confirmation_expires_at:
        if not order.completion_submitted_at:
            raise ValueError("用户确认截止时间不能为空。")
        from backoffice.operation_settings import platform_operation_rules

        order.confirmation_expires_at = order.completion_submitted_at + timedelta(
            days=platform_operation_rules()["provider_order_confirmation_timeout_days"]
        )
        order.save(update_fields=("confirmation_expires_at", "updated_at"))
    dedupe_key = task_dedupe_key(
        ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
        "provider_order",
        order.order_no,
    )
    task = ScheduledTask.objects.select_for_update().filter(dedupe_key=dedupe_key).first()
    if task is None:
        return register_provider_order_confirmation_timeout(order)
    if task.status == ScheduledTask.Status.PENDING:
        return register_provider_order_confirmation_timeout(order)
    if task.status != ScheduledTask.Status.CANCELLED:
        return task, False
    task.status = ScheduledTask.Status.PENDING
    task.scheduled_at = order.confirmation_expires_at
    task.available_at = order.confirmation_expires_at
    task.attempt_count = 0
    task.started_at = None
    task.finished_at = None
    task.last_error = ""
    task.result = {}
    task.save(
        update_fields=(
            "status", "scheduled_at", "available_at", "attempt_count", "started_at",
            "finished_at", "last_error", "result", "updated_at",
        )
    )
    return task, False


def mark_business_task_succeeded(*, task_type, business_type, business_key, result):
    now = timezone.now()
    return ScheduledTask.objects.filter(
        dedupe_key=task_dedupe_key(task_type, business_type, business_key),
        status__in=(ScheduledTask.Status.PENDING, ScheduledTask.Status.RUNNING),
    ).update(
        status=ScheduledTask.Status.SUCCEEDED,
        finished_at=now,
        last_error="",
        result=result,
        updated_at=now,
    )


def mark_provider_order_payment_expired(order_no: str, *, source: str):
    return mark_business_task_succeeded(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
        business_type="provider_order",
        business_key=order_no,
        result={"order_no": order_no, "action": "cancelled", "source": source},
    )


def mark_provider_acceptance_expired(order_no: str, *, source: str):
    return mark_business_task_succeeded(
        task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
        business_type="provider_order",
        business_key=order_no,
        result={"order_no": order_no, "action": "moved_to_support", "source": source},
    )


def mark_activity_publish_payment_expired(order_no: str, *, source: str):
    return mark_business_task_succeeded(
        task_type=ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY,
        business_type="activity_publish_payment",
        business_key=order_no,
        result={"order_no": order_no, "action": "cancelled", "source": source},
    )


def _mark_refund_task_succeeded(*, task_type, business_type, refund_no):
    now = timezone.now()
    return ScheduledTask.objects.filter(
        dedupe_key=task_dedupe_key(task_type, business_type, refund_no),
        status__in=(
            ScheduledTask.Status.PENDING,
            ScheduledTask.Status.FAILED,
        ),
    ).update(
        status=ScheduledTask.Status.SUCCEEDED,
        finished_at=now,
        last_error="",
        result={"refund_no": refund_no, "state": "succeeded"},
        updated_at=now,
    )


def mark_provider_order_refund_succeeded(refund_no: str):
    return _mark_refund_task_succeeded(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND,
        business_type="provider_order_refund",
        refund_no=refund_no,
    )


def mark_activity_participation_refund_succeeded(refund_no: str):
    return _mark_refund_task_succeeded(
        task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
        business_type="activity_participation_refund",
        refund_no=refund_no,
    )


def _requiring_task_synchronization(queryset, *, task_type, deadline_field):
    existing_task = ScheduledTask.objects.filter(
        task_type=task_type,
        business_type="provider_order",
        business_key=OuterRef("order_no"),
    ).order_by()
    return queryset.annotate(
        _scheduled_task_status=Subquery(existing_task.values("status")[:1]),
        _scheduled_task_deadline=Subquery(existing_task.values("scheduled_at")[:1]),
    ).filter(
        Q(_scheduled_task_status__isnull=True)
        | (
            Q(_scheduled_task_status=ScheduledTask.Status.PENDING)
            & (
                Q(**{f"{deadline_field}__isnull": True})
                | ~Q(_scheduled_task_deadline=F(deadline_field))
            )
        )
    )


def synchronize_provider_order_tasks(*, batch_size=TASK_SYNC_BATCH_SIZE) -> dict:
    from backoffice.operation_settings import platform_operation_rules
    from orders.models import ProviderOrder
    from orders.models import ProviderOrderRefundOrder
    from orders.models import ProviderOrderSettlement
    from orders.services import ensure_provider_order_settlement
    from providers.presence import operation_rules

    batch_size = max(1, min(int(batch_size), 5000))
    payment_created = 0
    acceptance_created = 0
    confirmation_created = 0
    settlement_created = 0
    settlement_task_created = 0
    payment_orders = (
        _requiring_task_synchronization(
            ProviderOrder.objects.filter(status=ProviderOrder.Status.PENDING_PAYMENT),
            task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
            deadline_field="payment_expires_at",
        )
        .only("order_no", "payment_expires_at")
        .order_by("id")[:batch_size]
    )
    for order in payment_orders:
        _, created = register_provider_order_payment_expiry(order)
        payment_created += int(created)

    acceptance_timeout = timedelta(minutes=operation_rules()["acceptance_timeout_minutes"])
    acceptance_orders = (
        _requiring_task_synchronization(
            ProviderOrder.objects.filter(
                status=ProviderOrder.Status.PENDING_ACCEPTANCE,
                paid_at__isnull=False,
            ),
            task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
            deadline_field="acceptance_expires_at",
        )
        .only("order_no", "paid_at", "acceptance_expires_at")
        .order_by("id")[:batch_size]
    )
    for order in acceptance_orders:
        if not order.acceptance_expires_at:
            order.acceptance_expires_at = order.paid_at + acceptance_timeout
            order.save(update_fields=("acceptance_expires_at", "updated_at"))
        _, created = register_provider_acceptance_timeout(order)
        acceptance_created += int(created)

    confirmation_timeout = timedelta(
        days=platform_operation_rules()["provider_order_confirmation_timeout_days"]
    )
    confirmation_orders = (
        _requiring_task_synchronization(
            ProviderOrder.objects.filter(
                status=ProviderOrder.Status.PENDING_CONFIRMATION,
                completion_submitted_at__isnull=False,
            ),
            task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
            deadline_field="confirmation_expires_at",
        )
        .only("order_no", "completion_submitted_at", "confirmation_expires_at")
        .order_by("id")[:batch_size]
    )
    for order in confirmation_orders:
        if not order.confirmation_expires_at:
            order.confirmation_expires_at = order.completion_submitted_at + confirmation_timeout
            order.save(update_fields=("confirmation_expires_at", "updated_at"))
        _, created = register_provider_order_confirmation_timeout(order)
        confirmation_created += int(created)

    confirmed_without_settlement = ProviderOrder.objects.filter(
        Q(customer_confirmed_at__isnull=False) | Q(auto_confirmed_at__isnull=False),
        settlement__isnull=True,
    ).only("order_no").order_by("id")[:batch_size]
    for order in confirmed_without_settlement:
        try:
            _, created = ensure_provider_order_settlement(order_no=order.order_no)
            settlement_created += int(created)
        except ValidationError:
            continue

    settlement_task_query = ProviderOrderSettlement.objects.filter(
        status=ProviderOrderSettlement.Status.RISK_FROZEN,
    ).select_related("order").order_by("id")[:batch_size]
    for settlement in settlement_task_query:
        task_exists = ScheduledTask.objects.filter(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
            business_type="provider_order",
            business_key=settlement.order.order_no,
            status__in=(ScheduledTask.Status.PENDING, ScheduledTask.Status.RUNNING),
        ).exists()
        if not task_exists:
            reopen_provider_order_settlement(settlement)
            settlement_task_created += 1
    refund_task_created = 0
    refund_task = ScheduledTask.objects.filter(
        task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND,
        business_type="provider_order_refund",
        business_key=OuterRef("refund_no"),
    )
    refunds = (
        ProviderOrderRefundOrder.objects.filter(
            status__in=(
                ProviderOrderRefundOrder.Status.PENDING,
                ProviderOrderRefundOrder.Status.PROCESSING,
                ProviderOrderRefundOrder.Status.FAILED,
            )
        )
        .annotate(_has_scheduled_task=Exists(refund_task))
        .filter(_has_scheduled_task=False)
        .select_related("order")
        .order_by("id")[:batch_size]
    )
    for refund in refunds:
        _, created = register_provider_order_refund(refund)
        refund_task_created += int(created)
    return {
        "payment_created": payment_created,
        "acceptance_created": acceptance_created,
        "confirmation_created": confirmation_created,
        "settlement_created": settlement_created,
        "settlement_task_created": settlement_task_created,
        "refund_task_created": refund_task_created,
    }


def _activities_missing_task(queryset, *, task_type):
    existing_task = ScheduledTask.objects.filter(
        task_type=task_type,
        business_type="activity",
        business_key=Cast(OuterRef("pk"), output_field=CharField()),
    )
    return queryset.annotate(
        _has_scheduled_task=Exists(existing_task)
    ).filter(_has_scheduled_task=False)


def synchronize_activity_tasks(*, batch_size=TASK_SYNC_BATCH_SIZE) -> dict:
    from activities.models import (
        Activity,
        ActivityParticipationPaymentOrder,
        ActivityParticipationRefundOrder,
        ActivityPublishOrder,
        ActivitySettlement,
    )
    from activities.services import ensure_activity_settlement

    batch_size = max(1, min(int(batch_size), 5000))
    payment_created = 0
    publish_payment_created = 0
    publish_payment_task = ScheduledTask.objects.filter(
        task_type=ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY,
        business_type="activity_publish_payment",
        business_key=OuterRef("order_no"),
    )
    pending_publish_payments = (
        ActivityPublishOrder.objects.filter(
            status=ActivityPublishOrder.Status.PENDING_PAYMENT,
        )
        .annotate(_has_scheduled_task=Exists(publish_payment_task))
        .filter(_has_scheduled_task=False)
        .select_related("activity")
        .order_by("id")[:batch_size]
    )
    for payment in pending_publish_payments:
        _, created = register_activity_publish_payment_expiry(payment)
        publish_payment_created += int(created)

    payment_existing = ScheduledTask.objects.filter(
        task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY,
        business_type="activity_participation",
        business_key=OuterRef("order_no"),
    )
    pending_payments = (
        ActivityParticipationPaymentOrder.objects.filter(
            status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
        )
        .annotate(_has_scheduled_task=Exists(payment_existing))
        .filter(_has_scheduled_task=False)
        .select_related("participation__activity")
        .order_by("id")[:batch_size]
    )
    for payment in pending_payments:
        _, created = register_activity_participation_payment_expiry(payment)
        payment_created += int(created)

    refund_task_created = 0
    refund_task = ScheduledTask.objects.filter(
        task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
        business_type="activity_participation_refund",
        business_key=OuterRef("refund_no"),
    )
    refunds = (
        ActivityParticipationRefundOrder.objects.filter(
            status__in=(
                ActivityParticipationRefundOrder.Status.PENDING,
                ActivityParticipationRefundOrder.Status.PROCESSING,
                ActivityParticipationRefundOrder.Status.FAILED,
            )
        )
        .annotate(_has_scheduled_task=Exists(refund_task))
        .filter(_has_scheduled_task=False)
        .select_related("activity", "participation", "payment_order")
        .order_by("id")[:batch_size]
    )
    for refund in refunds:
        _, created = register_activity_participation_refund(refund)
        refund_task_created += int(created)

    formation_created = 0
    formation_activities = (
        _activities_missing_task(
            Activity.objects.filter(
                status__in=(Activity.Status.RECRUITING, Activity.Status.FORMED),
            ),
            task_type=ScheduledTask.Type.ACTIVITY_FORMATION_DEADLINE,
        )
        .only("id", "title", "formation_deadline")
        .order_by("id")[:batch_size]
    )
    for activity in formation_activities:
        _, created = register_activity_formation_deadline(activity)
        formation_created += int(created)

    active_statuses = (
        Activity.Status.RECRUITING,
        Activity.Status.FORMED,
        Activity.Status.IN_PROGRESS,
    )
    start_created = 0
    start_activities = (
        _activities_missing_task(
            Activity.objects.filter(status__in=active_statuses),
            task_type=ScheduledTask.Type.ACTIVITY_START,
        )
        .only("id", "title", "starts_at")
        .order_by("id")[:batch_size]
    )
    for activity in start_activities:
        _, created = register_activity_start(activity)
        start_created += int(created)

    completion_created = 0
    completion_activities = (
        _activities_missing_task(
            Activity.objects.filter(status__in=active_statuses),
            task_type=ScheduledTask.Type.ACTIVITY_COMPLETION,
        )
        .only("id", "title", "ends_at")
        .order_by("id")[:batch_size]
    )
    for activity in completion_activities:
        _, created = register_activity_completion(activity)
        completion_created += int(created)

    settlement_created = 0
    completed_without_settlement = (
        Activity.objects.filter(
            status=Activity.Status.COMPLETED,
            settlement__isnull=True,
        )
        .only("id")
        .order_by("id")[:batch_size]
    )
    for activity in completed_without_settlement:
        try:
            _, created = ensure_activity_settlement(activity_id=activity.pk)
            settlement_created += int(created)
        except ValidationError:
            continue

    settlement_task_created = 0
    settlement_task = ScheduledTask.objects.filter(
        task_type=ScheduledTask.Type.ACTIVITY_SETTLEMENT,
        business_type="activity",
        business_key=Cast(OuterRef("activity_id"), output_field=CharField()),
    )
    settlements = (
        ActivitySettlement.objects.exclude(status=ActivitySettlement.Status.SETTLED)
        .annotate(_has_scheduled_task=Exists(settlement_task))
        .filter(_has_scheduled_task=False)
        .select_related("activity")
        .order_by("id")[:batch_size]
    )
    for settlement in settlements:
        _, created = register_activity_settlement(settlement)
        settlement_task_created += int(created)

    return {
        "publish_payment_created": publish_payment_created,
        "payment_created": payment_created,
        "refund_task_created": refund_task_created,
        "formation_created": formation_created,
        "start_created": start_created,
        "completion_created": completion_created,
        "settlement_created": settlement_created,
        "settlement_task_created": settlement_task_created,
    }


def synchronize_business_tasks(*, batch_size=TASK_SYNC_BATCH_SIZE) -> dict:
    return {
        "provider_orders": synchronize_provider_order_tasks(batch_size=batch_size),
        "activities": synchronize_activity_tasks(batch_size=batch_size),
    }


def _execute_provider_order_payment_expiry(task, now):
    from orders.services import expire_provider_order_payment

    outcome = expire_provider_order_payment(task.business_key, now=now)
    if outcome["state"] == "not_due":
        deadline = outcome["deadline"]
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={**outcome, "deadline": deadline.isoformat()},
            available_at=deadline,
        )
    if outcome["state"] == "expired":
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result={**outcome, "source": "task_worker"},
        )
    return TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED, result=outcome)


def _execute_provider_acceptance_timeout(task, now):
    from orders.services import expire_provider_acceptance

    outcome = expire_provider_acceptance(task.business_key, now=now)
    if outcome["state"] == "not_due":
        deadline = outcome["deadline"]
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={**outcome, "deadline": deadline.isoformat()},
            available_at=deadline,
        )
    if outcome["state"] == "expired":
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result={**outcome, "source": "task_worker"},
        )
    return TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED, result=outcome)


def _execute_provider_order_confirmation_timeout(task, now):
    from orders.services import auto_confirm_provider_order

    outcome = auto_confirm_provider_order(task.business_key, now=now)
    if outcome["state"] == "not_due":
        deadline = outcome["deadline"]
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={**outcome, "deadline": deadline.isoformat()},
            available_at=deadline,
        )
    if outcome["state"] == "expired":
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result={**outcome, "source": "task_worker"},
        )
    return TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED, result=outcome)


def _execute_provider_order_settlement(task, now):
    from orders.services import advance_provider_order_settlement

    outcome = advance_provider_order_settlement(order_no=task.business_key, now=now)
    if outcome["state"] == "not_due":
        deadline = outcome["deadline"]
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={**outcome, "deadline": deadline.isoformat()},
            available_at=deadline,
        )
    if outcome["state"] == "dispute_frozen":
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result=outcome,
            available_at=now + timedelta(minutes=5),
        )
    if outcome["state"] in ("settled", "cancelled"):
        return TaskExecutionOutcome(status=ScheduledTask.Status.SUCCEEDED, result=outcome)
    return TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED, result=outcome)


def _execute_provider_order_refund(task, now):
    from orders.models import ProviderOrderRefundOrder
    from orders.services import process_provider_order_refund

    refund = ProviderOrderRefundOrder.objects.filter(refund_no=task.business_key).first()
    if not refund:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.CANCELLED,
            result={"state": "missing", "refund_no": task.business_key},
        )
    refund, changed = process_provider_order_refund(task.business_key, now=now)
    return TaskExecutionOutcome(
        status=ScheduledTask.Status.SUCCEEDED,
        result={
            "state": refund.status,
            "refund_no": refund.refund_no,
            "order_no": refund.order.order_no,
            "changed": changed,
        },
    )


def _activity_result(activity, state):
    return {
        "state": state,
        "activity_id": activity.pk,
        "activity_status": activity.status,
    }


def _execute_activity_publish_payment_expiry(task, now):
    from activities.services import expire_activity_publish_payment

    outcome = expire_activity_publish_payment(order_no=task.business_key, now=now)
    if outcome["state"] == "not_due":
        deadline = outcome["deadline"]
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={**outcome, "deadline": deadline.isoformat()},
            available_at=deadline,
        )
    if outcome["state"] == "expired":
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result={**outcome, "source": "task_worker"},
        )
    return TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED, result=outcome)


def _execute_activity_participation_payment_expiry(task, now):
    from activities.services import expire_activity_participation_payment

    outcome = expire_activity_participation_payment(
        order_no=task.business_key,
        now=now,
    )
    if outcome["state"] == "not_due":
        deadline = outcome["deadline"]
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={**outcome, "deadline": deadline.isoformat()},
            available_at=deadline,
        )
    if outcome["state"] == "expired":
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result={**outcome, "source": "task_worker"},
        )
    return TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED, result=outcome)


def _execute_activity_participation_refund(task, now):
    from activities.models import ActivityParticipationRefundOrder
    from activities.services import process_activity_participation_refund

    refund = ActivityParticipationRefundOrder.objects.filter(
        refund_no=task.business_key
    ).first()
    if not refund:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.CANCELLED,
            result={"state": "missing", "refund_no": task.business_key},
        )
    refund, changed = process_activity_participation_refund(
        task.business_key, now=now
    )
    return TaskExecutionOutcome(
        status=ScheduledTask.Status.SUCCEEDED,
        result={
            "state": refund.status,
            "refund_no": refund.refund_no,
            "payment_order_no": refund.payment_order.order_no,
            "activity_id": refund.activity_id,
            "changed": changed,
        },
    )


def _resolve_activity_formation(*, activity_id, now):
    from activities.models import Activity
    from activities.services import fail_unformed_activity

    activity = Activity.objects.filter(pk=activity_id).first()
    if activity and (
        activity.status == Activity.Status.RECRUITING
        and activity.formation_deadline <= now
    ):
        activity, _ = fail_unformed_activity(activity_id=activity_id, now=now)
    return activity


def _execute_activity_formation_deadline(task, now):
    from activities.models import Activity
    from activities.services import fail_unformed_activity

    activity_id = int(task.business_key)
    activity = Activity.objects.filter(pk=activity_id).first()
    if not activity:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.CANCELLED,
            result={"state": "missing", "activity_id": activity_id},
        )
    if (
        activity.status == Activity.Status.RECRUITING
        and activity.formation_deadline > now
    ):
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={
                **_activity_result(activity, "not_due"),
                "deadline": activity.formation_deadline.isoformat(),
            },
            available_at=activity.formation_deadline,
        )
    activity, changed = fail_unformed_activity(activity_id=activity_id, now=now)
    if activity.status in (Activity.Status.FORMED, Activity.Status.FAILED_TO_FORM):
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result=_activity_result(
                activity,
                "formed" if activity.status == Activity.Status.FORMED else "failed_to_form",
            ) | {"changed": changed},
        )
    return TaskExecutionOutcome(
        status=ScheduledTask.Status.CANCELLED,
        result=_activity_result(activity, "inactive"),
    )


def _execute_activity_start(task, now):
    from activities.models import Activity
    from activities.services import start_formed_activity

    activity_id = int(task.business_key)
    activity = _resolve_activity_formation(activity_id=activity_id, now=now)
    if not activity:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.CANCELLED,
            result={"state": "missing", "activity_id": activity_id},
        )
    if activity.status == Activity.Status.FORMED and activity.starts_at > now:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={
                **_activity_result(activity, "not_due"),
                "deadline": activity.starts_at.isoformat(),
            },
            available_at=activity.starts_at,
        )
    activity, changed = start_formed_activity(activity_id=activity_id, now=now)
    if activity.status in (Activity.Status.IN_PROGRESS, Activity.Status.COMPLETED):
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result=_activity_result(activity, "started") | {"changed": changed},
        )
    return TaskExecutionOutcome(
        status=ScheduledTask.Status.CANCELLED,
        result=_activity_result(activity, "inactive"),
    )


def _execute_activity_completion(task, now):
    from activities.models import Activity
    from activities.services import (
        complete_started_activity,
        ensure_activity_settlement,
        start_formed_activity,
    )

    activity_id = int(task.business_key)
    activity = _resolve_activity_formation(activity_id=activity_id, now=now)
    if not activity:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.CANCELLED,
            result={"state": "missing", "activity_id": activity_id},
        )
    if activity.status == Activity.Status.FORMED and activity.starts_at <= now:
        activity, _ = start_formed_activity(activity_id=activity_id, now=now)
    if activity.status == Activity.Status.IN_PROGRESS and activity.ends_at > now:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result={
                **_activity_result(activity, "not_due"),
                "deadline": activity.ends_at.isoformat(),
            },
            available_at=activity.ends_at,
        )
    activity, changed = complete_started_activity(activity_id=activity_id, now=now)
    if activity.status == Activity.Status.COMPLETED:
        settlement, settlement_created = ensure_activity_settlement(
            activity_id=activity_id,
            now=now,
        )
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result=_activity_result(activity, "completed") | {
                "changed": changed,
                "settlement_no": settlement.settlement_no,
                "settlement_created": settlement_created,
            },
        )
    return TaskExecutionOutcome(
        status=ScheduledTask.Status.CANCELLED,
        result=_activity_result(activity, "inactive"),
    )


def _execute_activity_settlement(task, now):
    from activities.models import ActivitySettlement
    from activities.services import advance_activity_settlement

    settlement = (
        ActivitySettlement.objects.filter(activity_id=int(task.business_key))
        .select_related("activity")
        .first()
    )
    if not settlement:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.CANCELLED,
            result={"state": "missing", "activity_id": int(task.business_key)},
        )
    settlement, changed = advance_activity_settlement(
        settlement_id=settlement.pk,
        now=now,
    )
    result = {
        "state": settlement.status,
        "activity_id": settlement.activity_id,
        "settlement_no": settlement.settlement_no,
        "changed": changed,
    }
    if settlement.status == ActivitySettlement.Status.SETTLED:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.SUCCEEDED,
            result=result,
        )
    if settlement.status == ActivitySettlement.Status.DISPUTE_FROZEN:
        return TaskExecutionOutcome(
            status=ScheduledTask.Status.PENDING,
            result=result,
            available_at=now + timedelta(minutes=5),
        )
    deadline = activity_settlement_deadline(settlement)
    return TaskExecutionOutcome(
        status=ScheduledTask.Status.PENDING,
        result={**result, "deadline": deadline.isoformat()},
        available_at=deadline,
    )


TASK_HANDLERS = {
    ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY: (_execute_provider_order_payment_expiry),
    ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT: (_execute_provider_acceptance_timeout),
    ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT: (
        _execute_provider_order_confirmation_timeout
    ),
    ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT: _execute_provider_order_settlement,
    ScheduledTask.Type.PROVIDER_ORDER_REFUND: _execute_provider_order_refund,
    ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY: (
        _execute_activity_publish_payment_expiry
    ),
    ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY: (
        _execute_activity_participation_payment_expiry
    ),
    ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND: (
        _execute_activity_participation_refund
    ),
    ScheduledTask.Type.ACTIVITY_FORMATION_DEADLINE: _execute_activity_formation_deadline,
    ScheduledTask.Type.ACTIVITY_START: _execute_activity_start,
    ScheduledTask.Type.ACTIVITY_COMPLETION: _execute_activity_completion,
    ScheduledTask.Type.ACTIVITY_SETTLEMENT: _execute_activity_settlement,
}


def _release_stale_tasks(now):
    return ScheduledTask.objects.filter(
        status=ScheduledTask.Status.RUNNING,
        started_at__lte=now - TASK_LEASE_TIMEOUT,
    ).update(
        status=ScheduledTask.Status.PENDING,
        available_at=now,
        started_at=None,
        updated_at=now,
    )


def _claim_due_tasks(*, now, limit, task_types=None):
    with transaction.atomic():
        queryset = ScheduledTask.objects.select_for_update(skip_locked=True).filter(
            status=ScheduledTask.Status.PENDING,
            available_at__lte=now,
        )
        if task_types:
            queryset = queryset.filter(task_type__in=task_types)
        task_ids = list(
            queryset.order_by("available_at", "id").values_list("id", flat=True)[:limit]
        )
        if not task_ids:
            return []
        ScheduledTask.objects.filter(id__in=task_ids).update(
            status=ScheduledTask.Status.RUNNING,
            started_at=now,
            finished_at=None,
            attempt_count=F("attempt_count") + 1,
            updated_at=now,
        )
    return list(ScheduledTask.objects.filter(id__in=task_ids).order_by("available_at", "id"))


def _finish_task(task_id: int, outcome: TaskExecutionOutcome, now):
    with transaction.atomic():
        task = ScheduledTask.objects.select_for_update().get(id=task_id)
        if task.status != ScheduledTask.Status.RUNNING:
            return task.status
        task.status = outcome.status
        task.result = outcome.result
        task.last_error = ""
        if outcome.status == ScheduledTask.Status.PENDING:
            task.available_at = outcome.available_at or now
            task.started_at = None
            task.finished_at = None
        else:
            task.finished_at = now
        task.save(
            update_fields=(
                "status",
                "result",
                "last_error",
                "available_at",
                "started_at",
                "finished_at",
                "updated_at",
            )
        )
        return task.status


def _fail_task(task_id: int, exc: Exception, now):
    with transaction.atomic():
        task = ScheduledTask.objects.select_for_update().get(id=task_id)
        if task.status != ScheduledTask.Status.RUNNING:
            return task.status
        task.last_error = f"{exc.__class__.__name__}: {exc}"[:4000]
        task.result = {}
        task.started_at = None
        if task.attempt_count >= task.max_attempts:
            task.status = ScheduledTask.Status.FAILED
            task.finished_at = now
        else:
            multiplier = 2 ** max(task.attempt_count - 1, 0)
            task.status = ScheduledTask.Status.PENDING
            task.available_at = now + TASK_RETRY_BASE_DELAY * multiplier
            task.finished_at = None
        task.save(
            update_fields=(
                "status",
                "available_at",
                "started_at",
                "finished_at",
                "last_error",
                "result",
                "updated_at",
            )
        )
        return task.status


def process_due_tasks(*, limit=100, task_types=None, now=None) -> dict:
    now = now or timezone.now()
    released = _release_stale_tasks(now)
    tasks = _claim_due_tasks(
        now=now,
        limit=max(1, min(int(limit), 500)),
        task_types=task_types,
    )
    result = {
        "released": released,
        "claimed": len(tasks),
        "succeeded": 0,
        "cancelled": 0,
        "rescheduled": 0,
        "retried": 0,
        "failed": 0,
    }
    for task in tasks:
        try:
            handler = TASK_HANDLERS[task.task_type]
            final_status = _finish_task(task.id, handler(task, now), now)
            if final_status == ScheduledTask.Status.SUCCEEDED:
                result["succeeded"] += 1
            elif final_status == ScheduledTask.Status.CANCELLED:
                result["cancelled"] += 1
            elif final_status == ScheduledTask.Status.PENDING:
                result["rescheduled"] += 1
        except Exception as exc:  # Celery must keep processing the remaining batch.
            final_status = _fail_task(task.id, exc, now)
            result["failed" if final_status == ScheduledTask.Status.FAILED else "retried"] += 1
    return result


def retry_failed_task(task):
    if task.status != ScheduledTask.Status.FAILED:
        raise ValueError("只有执行失败的任务可以重试。")
    now = timezone.now()
    task.status = ScheduledTask.Status.PENDING
    task.available_at = now
    task.attempt_count = 0
    task.started_at = None
    task.finished_at = None
    task.last_error = ""
    task.result = {}
    task.save(
        update_fields=(
            "status",
            "available_at",
            "attempt_count",
            "started_at",
            "finished_at",
            "last_error",
            "result",
            "updated_at",
        )
    )
    return task
