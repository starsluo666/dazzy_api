import uuid

from django.conf import settings
from django.db import models


class GrowthCampaignConfig(models.Model):
    """Singleton configuration for the referral and newcomer campaign."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    newcomer_gift_enabled = models.BooleanField("启用新人礼包", default=False)
    invitation_enabled = models.BooleanField("启用邀请奖励", default=False)
    registration_reward_template = models.ForeignKey(
        "orders.CouponTemplate",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="growth_registration_reward_configs",
        verbose_name="有效注册奖励券",
    )
    first_order_reward_template = models.ForeignKey(
        "orders.CouponTemplate",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="growth_first_order_reward_configs",
        verbose_name="首单完成奖励券",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="updated_growth_campaign_configs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_campaign_config"
        verbose_name = "拉新活动配置"
        verbose_name_plural = verbose_name
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=1),
                name="growth_campaign_config_singleton",
            )
        ]


class NewcomerGiftItem(models.Model):
    config = models.ForeignKey(
        GrowthCampaignConfig,
        on_delete=models.CASCADE,
        related_name="newcomer_gift_items",
    )
    template = models.ForeignKey(
        "orders.CouponTemplate",
        on_delete=models.PROTECT,
        related_name="newcomer_gift_items",
    )
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_newcomer_gift_item"
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("config", "template"),
                name="uniq_growth_gift_config_template",
            )
        ]


class Invitation(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    inviter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sent_invitations",
    )
    invitee = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="received_invitation",
    )
    invite_code_snapshot = models.CharField(max_length=36)
    registration_reward_coupon = models.OneToOneField(
        "orders.UserCoupon",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="registration_invitation_reward",
    )
    first_order_reward_coupon = models.OneToOneField(
        "orders.UserCoupon",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="first_order_invitation_reward",
    )
    first_completed_order = models.OneToOneField(
        "orders.ProviderOrder",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="invitation_reward",
    )
    registered_at = models.DateTimeField()
    first_order_completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_invitation"
        ordering = ("-registered_at", "-id")
        indexes = [
            models.Index(fields=("inviter", "-registered_at"), name="growth_inviter_registered_idx"),
        ]


class NewcomerGiftGrant(models.Model):
    recipient = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="newcomer_gift_grant",
    )
    invitation = models.OneToOneField(
        Invitation,
        on_delete=models.PROTECT,
        related_name="newcomer_gift_grant",
    )
    coupons = models.ManyToManyField(
        "orders.UserCoupon",
        related_name="newcomer_gift_grants",
        blank=True,
    )
    template_snapshot = models.JSONField(default=list)
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_newcomer_gift_grant"
