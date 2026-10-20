"""Idempotent daily reads; writes are strictly limited to reconciliation tables."""
import uuid
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from orders.distributions import _scope
from orders.huifu import HuifuPaymentConfig, _canonical_digest
from .comparison import normalize, compare, issue_identity
from .gateway import BillError, BillPending, fetch_bill
from .local_records import records_for
from .models import BILL_TYPES, ReconciliationBatch as Batch, ReconciliationIssue as Issue, ReconciliationIssueEvent as Event

ZONE = ZoneInfo("Asia/Shanghai")
ERRORS = {
    "bill_not_ready": "渠道账单尚未生成，不能认定当日无交易。",
    "channel_unverified": "账单查询结果未能核实，请检查渠道权限及原请求。",
    "unsupported_schema": "账单表头与已核验模板不一致，需核对格式后重新对账。",
    "bill_merchant_mismatch": "账单记录的商户范围不一致，需财务核查。",
    "download_failed": "账单下载未完成，请核查下载域名与网络。",
}


def start_date():
    try:
        return date.fromisoformat(settings.HUIFU_RECONCILIATION_START_DATE)
    except (TypeError, ValueError):
        return None


def event(issue, action, note=""):
    Event.objects.create(issue=issue, action=action, revision=issue.revision, note=note)


def save_findings(batch, findings, *, complete):
    """Caller holds batch lock. Same evidence does not spam unread work."""
    keys = set()
    for item in findings:
        key, fingerprint = issue_identity(item), _canonical_digest(item)
        if key in keys:
            continue
        keys.add(key)
        issue, created = Issue.objects.get_or_create(
            batch=batch, issue_key=key,
            defaults={**item, "fingerprint": fingerprint},
        )
        if created:
            event(issue, "detected")
        elif issue.fingerprint != fingerprint or issue.status == "cleared":
            for field, value in item.items():
                setattr(issue, field, value)
            issue.fingerprint = fingerprint
            issue.status = "open"
            issue.submitted_by = None
            issue.revision += 1
            issue.save()
            event(issue, "evidence_changed")
    if complete:
        for issue in batch.issues.exclude(issue_key__in=keys).exclude(status__in=("resolved", "cleared")):
            issue.status = "cleared"
            issue.revision += 1
            issue.save()
            event(issue, "rechecked", "重新对账已无此差异；未改变任何资金状态。")


def run_batch(batch_id, *, config=None):
    if not settings.HUIFU_RECONCILIATION_ENABLED:
        return False
    if transaction.get_connection().in_atomic_block:
        raise BillError("outer_transaction")
    config = config or HuifuPaymentConfig.from_settings()
    now, today = timezone.now(), timezone.localtime(timezone.now(), ZONE).date()
    with transaction.atomic():
        batch = Batch.objects.select_for_update().get(pk=batch_id)
        start = start_date()
        if batch.scope != _scope(config) or start is None or not start <= batch.data_date < today:
            raise BillError("scope_or_date")
        if batch.lease_until and batch.lease_until > now:
            return False
        if batch.next_attempt_at and batch.next_attempt_at > now:
            return False
        if batch.attempts_date == today and batch.attempts >= 3:
            return False
        batch.attempts = batch.attempts + 1 if batch.attempts_date == today else 1
        batch.attempts_date = today
        batch.status = "running"
        batch.run_token = uuid.uuid4()
        batch.lease_until = now + timedelta(minutes=30)
        batch.next_attempt_at = now + timedelta(hours=2)
        batch.save()
        token = batch.run_token
    try:
        raw, digest = fetch_bill(config, batch.data_date, batch.bill_type)
        channel = normalize(raw, batch.bill_type, config.merchant_id)
        local = records_for(config, batch.data_date, batch.bill_type, {row["key"] for row in channel})
        findings, summary = compare(channel, local, batch.bill_type)
        status, code, complete = "differences" if findings else "matched", "", True
    except Exception as exc:
        code = exc.code if isinstance(exc, BillError) else "internal_error"
        status, digest, summary, complete = "waiting" if isinstance(exc, BillPending) else "failed", "", {}, False
        findings = [{
            "category": "batch_incomplete", "reference": batch.data_date.isoformat(),
            "message": ERRORS.get(code, "本次对账未完成，请核对账单范围、格式或任务运行情况。"),
            "evidence": {"code": code},
        }]
    with transaction.atomic():
        batch = Batch.objects.select_for_update().get(pk=batch_id)
        if batch.run_token != token:
            return False  # Expired worker may not overwrite a newer run.
        save_findings(batch, findings, complete=complete)
        batch.status, batch.error_code = status, code
        batch.checked_at, batch.lease_until = timezone.now(), None
        if complete:
            batch.evidence_digest, batch.summary = digest, summary
        batch.save()
    return True


def ensure_day(day, config):
    return [Batch.objects.get_or_create(scope=_scope(config), data_date=day, bill_type=kind)[0]
            for kind, _ in BILL_TYPES]


def daily():
    if not settings.HUIFU_RECONCILIATION_ENABLED or start_date() is None:
        return {"disabled": True}
    now = timezone.localtime(timezone.now(), ZONE)
    # Current official API: cash bill batch at 17:15, allow >1 h. Conservative for all types.
    if now.time() < time(18, 30):
        return {"waiting_for_bill_window": True}
    config = HuifuPaymentConfig.from_settings()
    begin = max(start_date(), now.date() - timedelta(days=7))
    for offset in range((now.date() - begin).days):
        ensure_day(begin + timedelta(days=offset), config)
    # Retry recent days to catch late files/cross-day refunds. Older incomplete
    # batches remain visible; daily limits are persisted across worker restarts.
    candidates = Batch.objects.filter(scope=_scope(config), data_date__gte=begin, data_date__lt=now.date()).order_by("checked_at", "data_date", "pk")
    count = 0
    for batch in candidates:
        if batch.status in ("matched", "differences") and batch.attempts_date == now.date():
            continue
        count += int(run_batch(batch.pk, config=config))
    return {"checked": count}
