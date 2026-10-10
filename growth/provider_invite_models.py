"""Provider recruitment rewards: manual finance ledger, NOT an order wallet."""

import secrets
import uuid

from django.conf import settings
from django.db import models


def invitation_code():
    return secrets.token_hex(6).upper()


class ProviderInviteConfig(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    store_reward_amount = models.PositiveIntegerField(default=0)
    provider_reward_amount = models.PositiveIntegerField(default=0)
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
    code = models.CharField(max_length=12, default=invitation_code, unique=True, editable=False)
    kind = models.CharField(max_length=16, choices=Kind)
    name = models.CharField(max_length=80)
    provider_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="provider_invite_source",
    )
    active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="store", provider_user__isnull=True)
                    | models.Q(kind="provider", provider_user__isnull=False)
                ),
                name="provider_invite_source_kind_owner",
            )
        ]


class ProviderInviteReward(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待财务审核"
        APPROVED = "approved", "待人工打款"
        REJECTED = "rejected", "审核不通过"
        PAID = "paid", "已人工打款"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source = models.ForeignKey(
        ProviderInviteSource, on_delete=models.PROTECT, related_name="rewards"
    )
    # Permanent unique identity: a new phone account can qualify only once.
    invitee = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="provider_invite_reward"
    )
    profile = models.OneToOneField("providers.ProviderProfile", on_delete=models.PROTECT)
    source_name_snapshot = models.CharField(max_length=80)
    code_snapshot = models.CharField(max_length=12)
    amount = models.PositiveIntegerField()
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
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("status", "created_at"))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="provider_invite_reward_positive"
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="paid")
                    | (
                        models.Q(
                            paid_by__isnull=False,
                            paid_at__isnull=False,
                            transfer_reference__isnull=False,
                        )
                        & ~models.Q(transfer_reference="")
                    )
                ),
                name="provider_invite_paid_evidence",
            ),
        ]
