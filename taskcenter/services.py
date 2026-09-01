from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone

from .models import ScheduledTask


TASK_LEASE_TIMEOUT = timedelta(minutes=5)
TASK_RETRY_BASE_DELAY = timedelta(minutes=1)
TASK_SYNC_BATCH_SIZE = 500


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
    from orders.models import ProviderOrder
    from providers.presence import operation_rules

    batch_size = max(1, min(int(batch_size), 5000))
    payment_created = 0
    acceptance_created = 0
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
    return {
        "payment_created": payment_created,
        "acceptance_created": acceptance_created,
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


TASK_HANDLERS = {
    ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY: (_execute_provider_order_payment_expiry),
    ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT: (_execute_provider_acceptance_timeout),
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
