import uuid

from django.conf import settings
from django.db import models


BILL_TYPES = (
    ("TRADE_BILL", "交易及退款账单"),
    ("SPLIT_BILL", "分账账单"),
    ("WITHDRAWAL_BILL", "出金账单"),
    ("SETTLE_BILL", "商户结算账单"),
    ("SETTLE_USER_BILL", "用户结算账单"),
)


class ReconciliationBatch(models.Model):
    """Diagnostic data only; never a payment authority or replacement ledger."""

    class Status(models.TextChoices):
        PENDING = "pending", "待对账"
        RUNNING = "running", "对账中"
        WAITING = "waiting", "账单未就绪"
        FAILED = "failed", "对账未完成"
        MATCHED = "matched", "已核对项一致"
        DIFFERENCES = "differences", "存在差异或待核查项"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    scope = models.CharField(max_length=64)
    data_date = models.DateField()
    bill_type = models.CharField(max_length=32, choices=BILL_TYPES)
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    attempts_date = models.DateField(null=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    run_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    next_attempt_at = models.DateTimeField(null=True)
    checked_at = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=40, blank=True)
    summary = models.JSONField(default=dict)
    evidence_digest = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("scope", "data_date", "bill_type"), name="unique_daily_channel_bill"),
        ]
        indexes = [models.Index(fields=("status", "next_attempt_at"), name="recon_due_idx")]


class ReconciliationIssue(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "待核查"
        PROCESSING = "processing", "核查中"
        REVIEW = "review", "待复核"
        RESOLVED = "resolved", "人工核查已结案"
        CLEARED = "cleared", "重新对账已一致"

    batch = models.ForeignKey(ReconciliationBatch, on_delete=models.PROTECT, related_name="issues")
    issue_key = models.CharField(max_length=64)
    category = models.CharField(max_length=40)
    reference = models.CharField(max_length=128, blank=True)
    message = models.CharField(max_length=250)
    # Whitelisted diagnostic fields; never source CSV, signed URLs, card/account data.
    evidence = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status, default=Status.OPEN)
    revision = models.PositiveIntegerField(default=1)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("batch", "issue_key"), name="unique_bill_difference")]
        indexes = [models.Index(fields=("status", "created_at"), name="recon_issue_work_idx")]


class ReconciliationIssueEvent(models.Model):
    issue = models.ForeignKey(ReconciliationIssue, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True)
    action = models.CharField(max_length=32)
    note = models.CharField(max_length=1000, blank=True)
    evidence_reference = models.CharField(max_length=200, blank=True)
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
