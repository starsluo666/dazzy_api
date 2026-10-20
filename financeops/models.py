import uuid

from django.conf import settings
from django.db import models


class PaymentJobHeartbeat(models.Model):
    key = models.CharField(max_length=80, unique=True)
    run_token = models.UUIDField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    succeeded_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    error_code = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class FinanceRecoveryCase(models.Model):
    class Kind(models.TextChoices):
        DISTRIBUTION = "distribution", "订单渠道出款"
        WITHDRAWAL = "withdrawal", "达人提现"
        WALLET = "wallet", "达人收入账本"
        POOL = "pool", "平台余额付款暂停"

    class Status(models.TextChoices):
        OPEN = "open", "待核查"
        REVIEW = "review", "待独立复核"
        RESOLVED = "resolved", "已核查结案（未解除限制）"
        RESTORED = "restored", "已恢复后续处理"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    kind = models.CharField(max_length=20, choices=Kind)
    target_id = models.PositiveBigIntegerField()
    reference = models.CharField(max_length=128)
    status = models.CharField(max_length=20, choices=Status, default=Status.OPEN)
    revision = models.PositiveIntegerField(default=1)
    resolution = models.CharField(max_length=20, default="record")
    # Fingerprints only, never raw account/card/channel response data.
    submitted_fingerprint = models.CharField(max_length=64, blank=True)
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    run_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    queried_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=("kind", "target_id"), condition=models.Q(status__in=("open", "review")),
            name="finance_recovery_one_active_target",
        )]


class FinanceRecoveryEvent(models.Model):
    case = models.ForeignKey(FinanceRecoveryCase, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=30)
    note = models.CharField(max_length=1000, blank=True)
    evidence_reference = models.CharField(max_length=200, blank=True)
    verification = models.JSONField(default=dict, editable=False)
    revision = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
