"""Provider recruitment attribution, reward rules and channel payments."""

import secrets
import uuid

from django.conf import settings
from django.db import IntegrityError, models, router, transaction


INVITATION_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def invitation_code():
    return "".join(secrets.choice(INVITATION_CODE_ALPHABET) for _ in range(4))


class ProviderInviteConfig(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    store_reward_amount = models.PositiveIntegerField(default=0)
    provider_reward_amount = models.PositiveIntegerField(default=0)
    store_registration_enabled = models.BooleanField(default=True)
    provider_registration_enabled = models.BooleanField(default=True)
    store_first_order = models.JSONField(default=dict, blank=True)
    provider_first_order = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="provider_invite_singleton")
        ]


class ProviderInviteSource(models.Model):
    class Kind(models.TextChoices):
        STORE = "store", "门店"
        PROVIDER = "provider", "达人"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code = models.CharField(max_length=4, default=invitation_code, unique=True, editable=False)
    legacy_code = models.CharField(max_length=12, unique=True, null=True, blank=True, editable=False)
    kind = models.CharField(max_length=16, choices=Kind)
    name = models.CharField(max_length=80)
    provider_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="provider_invite_source",
    )
    store_user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="store_invite_source",
    )
    active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            return super().save(*args, **kwargs)
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        for _ in range(32):
            try:
                # A savepoint keeps surrounding registration transactions usable on collision.
                with transaction.atomic(using=using):
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not type(self).objects.using(using).filter(code=self.code).exists():
                    raise
                self.code = invitation_code()
        from rest_framework.exceptions import ValidationError
        raise ValidationError("邀请码生成繁忙，请稍后重试。")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="store", provider_user__isnull=True)
                    | models.Q(kind="provider", provider_user__isnull=False, store_user__isnull=True)
                ),
                name="provider_invite_source_kind_owner",
            )
        ]


class ProviderInviteAttribution(models.Model):
    profile = models.OneToOneField("providers.ProviderProfile", on_delete=models.PROTECT)
    source = models.ForeignKey(ProviderInviteSource, on_delete=models.PROTECT)
    source_name_snapshot = models.CharField(max_length=80)
    code_snapshot = models.CharField(max_length=12)
    first_order_rule = models.JSONField(default=dict, blank=True)
    config_revision = models.PositiveIntegerField()
    first_order = models.OneToOneField(
        "orders.ProviderOrder", on_delete=models.PROTECT, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)


class ProviderInviteReward(models.Model):
    class Kind(models.TextChoices):
        REGISTRATION = "registration", "注册奖励"
        FIRST_ORDER = "first_order", "首单奖励"

    class PayoutMethod(models.TextChoices):
        MANUAL = "manual", "人工转账"
        BALANCE = "balance", "汇付余额"

    class Status(models.TextChoices):
        PENDING = "pending", "待财务审核"
        APPROVED = "approved", "待人工打款"
        REJECTED = "rejected", "审核不通过"
        PAID = "paid", "已人工打款"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source = models.ForeignKey(
        ProviderInviteSource, on_delete=models.PROTECT, related_name="rewards"
    )
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="provider_invite_rewards"
    )
    profile = models.ForeignKey("providers.ProviderProfile", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind, default=Kind.REGISTRATION)
    payout_method = models.CharField(max_length=10, choices=PayoutMethod, default=PayoutMethod.MANUAL)
    order = models.OneToOneField("orders.ProviderOrder", null=True, blank=True, on_delete=models.PROTECT)
    calculation = models.JSONField(default=dict, blank=True)
    source_name_snapshot = models.CharField(max_length=80)
    code_snapshot = models.CharField(max_length=12)
    amount = models.PositiveBigIntegerField()
    config_revision = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    review_note = models.CharField(max_length=500, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reviewed_provider_invites",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="paid_provider_invites",
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    # One transfer per reward in phase one; finance must not reuse a receipt.
    transfer_reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
    payout_note = models.CharField(max_length=500, blank=True)
    payout_checked_at = models.DateTimeField(null=True)
    payout_blocker = models.CharField(max_length=120, blank=True)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("status", "created_at"))]
        constraints = [
            models.UniqueConstraint(fields=("invitee", "kind"), name="provider_invite_reward_event_unique"),
            models.CheckConstraint(
                condition=(models.Q(kind="registration", order__isnull=True)
                           | models.Q(kind="first_order", order__isnull=False)),
                name="provider_invite_reward_event_order",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="provider_invite_reward_positive"
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="paid")
                    | (
                        models.Q(
                            paid_at__isnull=False,
                            transfer_reference__isnull=False,
                        )
                        & (models.Q(paid_by__isnull=False) | models.Q(payout_method="balance"))
                        & ~models.Q(transfer_reference="")
                    )
                ),
                name="provider_invite_paid_evidence",
            ),
        ]


class ProviderInvitePayout(models.Model):
    class Status(models.TextChoices):
        SUBMITTING = "submitting", "汇付请求已登记"
        PROCESSING = "processing", "汇付处理中"
        UNKNOWN = "unknown", "汇付结果待核实"
        REVIEW = "review", "待人工核账"
        SUCCEEDED = "succeeded", "已入汇付余额"
        FAILED = "failed", "汇付付款失败"

    reward = models.OneToOneField(ProviderInviteReward, on_delete=models.PROTECT, related_name="payout")
    pool = models.ForeignKey(
        "orders.ProviderBalancePaymentPool", on_delete=models.PROTECT, related_name="invite_payments",
    )
    req_date = models.CharField(max_length=8, editable=False)
    req_seq_id = models.CharField(max_length=32, unique=True, editable=False)
    snapshot = models.JSONField(editable=False)
    status = models.CharField(max_length=16, choices=Status, default=Status.SUBMITTING, db_index=True)
    funds_released = models.BooleanField(default=False)
    gateway_trade_no = models.CharField(max_length=128, blank=True)
    fee_amount = models.PositiveBigIntegerField(null=True)
    response_digest = models.CharField(max_length=64, blank=True)
    last_queried_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
