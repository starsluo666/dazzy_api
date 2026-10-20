"""Durable task progress, independently readable when beat/worker stops.

The token fences *heartbeat writes*. Business idempotency still belongs to the
existing payment executors. We do not automatically replay payments or tasks.
"""
from datetime import time, timedelta
from functools import wraps
import hashlib
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import PaymentJobHeartbeat as Heartbeat

# label, maximum quiet interval, maximum execution interval, enable setting
JOBS = {
    "providers.process_income_transfers": ("渠道出款与提现查单", 300, 900, "HUIFU_PROVIDER_INCOME_JOBS_ENABLED"),
    "wallets.reconcile_pending_recharges": ("充值补偿查单", 300, 900, "HUIFU_PAYMENT_ENABLED"),
    "reconciliation.daily_bills": ("每日对账调度", 5400, 1800, "HUIFU_RECONCILIATION_ENABLED"),
    "taskcenter.process_due_scheduled_tasks": ("到期业务任务执行", 180, 900, None),
    "taskcenter.synchronize_scheduled_tasks": ("业务任务补建", 900, 900, None),
}


def monitored(key):
    def decorate(func):
        @wraps(func)
        def run(*args, **kwargs):
            now, token = timezone.now(), uuid.uuid4()
            with transaction.atomic():
                Heartbeat.objects.get_or_create(key=key)
                row = Heartbeat.objects.select_for_update().get(key=key)
                if row.lease_until and row.lease_until > now:
                    return {"monitor_busy": True}
                row.run_token, row.started_at = token, now
                row.lease_until = now + timedelta(seconds=JOBS[key][2])
                row.save()
            try:
                result = func(*args, **kwargs)
            except Exception:
                Heartbeat.objects.filter(key=key, run_token=token).update(
                    finished_at=timezone.now(), lease_until=None, error_code="task_failed",
                )
                raise
            # A disabled/misconfigured job must not get a healthy success timestamp.
            disabled = isinstance(result, dict) and result.get("disabled")
            values = {"finished_at": timezone.now(), "lease_until": None,
                      "error_code": "task_disabled" if disabled else ""}
            if not disabled:
                values["succeeded_at"] = values["finished_at"]
            Heartbeat.objects.filter(key=key, run_token=token).update(**values)
            return result
        return run
    return decorate


def health(now=None):
    now = now or timezone.now()
    stored = {row.key: row for row in Heartbeat.objects.filter(key__in=JOBS)}
    result = []
    for key, (label, quiet, runtime, flag) in JOBS.items():
        row = stored.get(key)
        enabled = flag is None or getattr(settings, flag, False)
        state, reason = "healthy", "最近执行正常；不代表每笔渠道交易均已成功。"
        if not enabled:
            state, reason = "disabled", "对应业务开关未启用。"
        elif row is None:
            state, reason = "missing", "尚无任务记录，请检查数据库迁移与任务部署。"
        elif row.lease_until and row.lease_until <= now:
            state, reason = "stuck", "任务超过执行时限，请检查 worker；不要直接重发资金请求。"
        elif row.error_code:
            state, reason = "failed", "最近执行失败或提前停用，请检查配置及脱敏日志。"
        elif now - (row.succeeded_at or row.created_at) > timedelta(seconds=quiet + (runtime if row.lease_until else 0)):
            state, reason = "stale", "已超过预期执行间隔，请检查 beat、worker 与任务队列。"
        elif row.lease_until:
            state, reason = "running", "任务执行中。"
        elif not row.succeeded_at:
            state, reason = "waiting", "等待部署后的首次执行。"
        if enabled and key == "reconciliation.daily_bills" and state in ("healthy", "running"):
            coverage = bill_coverage(now)
            if coverage:
                state, reason = "coverage", coverage
        result.append({
            "id": row.pk if row else None, "key": key, "label": label, "state": state, "reason": reason,
            "started_at": row.started_at if row else None, "finished_at": row.finished_at if row else None,
            "succeeded_at": row.succeeded_at if row else None, "quiet_seconds": quiet,
            "alert": state in ("missing", "stuck", "failed", "stale", "coverage"),
            # A continuing bill-coverage problem does not become unread on
            # every healthy scheduler heartbeat. Other incidents reset after success.
            "event_version": hashlib.sha256((
                reason if state == "coverage" else f"{state}:{row.succeeded_at or row.created_at}" if row else "missing"
            ).encode()).hexdigest(),
        })
    return result


def unhealthy_ids(now=None):
    return [item["id"] for item in health(now) if item["alert"] and item["id"]]


def bill_coverage(now):
    from reconciliation.models import BILL_TYPES, ReconciliationBatch
    from reconciliation.services import ZONE, start_date
    from orders.huifu import HuifuPaymentConfig
    from orders.distributions import _scope

    begin = start_date()
    if begin is None:
        return "每日对账起始日期未正确配置。"
    local = timezone.localtime(now, ZONE)
    # Allow two hours after the 18:30 daily window; before then check the
    # preceding due day rather than reporting today's not-yet-due bills.
    day = local.date() - timedelta(days=1 if local.time() >= time(20, 30) else 2)
    if day < begin:
        return ""
    try:
        scope = _scope(HuifuPaymentConfig.from_settings())
    except Exception:
        return "每日对账渠道配置尚未就绪。"
    complete = set(ReconciliationBatch.objects.filter(
        scope=scope, data_date=day, status__in=("matched", "differences"),
    ).values_list("bill_type", flat=True))
    if any(kind not in complete for kind, _ in BILL_TYPES):
        return f"{day.isoformat()} 的渠道账单尚未完整核对，请检查每日对账。"
    return ""
