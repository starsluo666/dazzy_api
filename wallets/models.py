import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class UserWallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet",
        verbose_name="用户",
    )
    available_balance = models.PositiveBigIntegerField("可用余额（分）", default=0)
    frozen_balance = models.PositiveBigIntegerField("冻结余额（分）", default=0)
    version = models.PositiveBigIntegerField("账本版本", default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_wallet"
        verbose_name = "用户钱包"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user_id} / {self.available_balance}"


class WalletLedgerEntry(models.Model):
    class EntryType(models.TextChoices):
        RECHARGE = "recharge", "充值入账"
        PAYMENT_HOLD = "payment_hold", "支付冻结"
        PAYMENT_CONSUME = "payment_consume", "支付扣款"
        PAYMENT_RELEASE = "payment_release", "支付解冻"
        REFUND = "refund", "退款入账"
        ADMIN_ADJUSTMENT = "admin_adjustment", "后台调账"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    wallet = models.ForeignKey(
        UserWallet,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
        verbose_name="钱包",
    )
    entry_type = models.CharField("流水类型", max_length=24, choices=EntryType)
    available_delta = models.BigIntegerField("可用余额变动（分）", default=0)
    frozen_delta = models.BigIntegerField("冻结余额变动（分）", default=0)
    available_balance_after = models.PositiveBigIntegerField("变动后可用余额（分）")
    frozen_balance_after = models.PositiveBigIntegerField("变动后冻结余额（分）")
    idempotency_key = models.CharField("幂等键", max_length=160, unique=True)
    reference_type = models.CharField("关联类型", max_length=40)
    reference_no = models.CharField("关联单号", max_length=64)
    description = models.CharField("说明", max_length=255, blank=True)
    metadata = models.JSONField("扩展信息", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wallet_ledger_entry"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("wallet", "-created_at"), name="wallet_ledger_owner_idx"),
            models.Index(fields=("reference_type", "reference_no"), name="wallet_ledger_ref_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(available_delta=0, frozen_delta=0),
                name="wallet_ledger_nonzero_delta",
            )
        ]
        verbose_name = "钱包流水"
        verbose_name_plural = verbose_name


class RechargeCampaign(models.Model):
    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    is_enabled = models.BooleanField("开放充值", default=False)
    unit_face_amount = models.PositiveBigIntegerField("单张面值（分）", default=100000)
    max_quantity_per_order = models.PositiveSmallIntegerField("单次最多购买张数", default=10)
    rules_text = models.CharField(
        "充值说明",
        max_length=500,
        default="充值余额仅限平台消费，不可提现；退款按订单原支付构成退回。",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="updated_recharge_campaigns",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_recharge_campaign"
        constraints = [
            models.CheckConstraint(
                condition=Q(singleton_key=1), name="wallet_recharge_singleton_key"
            ),
            models.CheckConstraint(
                condition=Q(unit_face_amount__gt=0), name="wallet_recharge_unit_positive"
            ),
            models.CheckConstraint(
                condition=Q(max_quantity_per_order__gte=1)
                & Q(max_quantity_per_order__lte=99),
                name="wallet_recharge_quantity_valid",
            ),
        ]
        verbose_name = "充值活动配置"
        verbose_name_plural = verbose_name

    @classmethod
    def current(cls):
        campaign, _ = cls.objects.get_or_create(singleton_key=1)
        return campaign


class RechargeDiscountTier(models.Model):
    campaign = models.ForeignKey(
        RechargeCampaign,
        on_delete=models.CASCADE,
        related_name="discount_tiers",
        verbose_name="充值活动",
    )
    min_quantity = models.PositiveSmallIntegerField("起始张数")
    discount_rate_bps = models.PositiveSmallIntegerField(
        "折扣比例（万分比）", default=10000
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_recharge_discount_tier"
        ordering = ("min_quantity", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("campaign", "min_quantity"),
                name="uniq_recharge_campaign_quantity",
            ),
            models.CheckConstraint(
                condition=Q(min_quantity__gte=1) & Q(min_quantity__lte=99),
                name="recharge_tier_quantity_valid",
            ),
            models.CheckConstraint(
                condition=Q(discount_rate_bps__gte=1)
                & Q(discount_rate_bps__lte=10000),
                name="recharge_tier_discount_valid",
            ),
        ]
        verbose_name = "充值折扣档位"
        verbose_name_plural = verbose_name


def generate_recharge_order_no():
    return f"WRO{uuid.uuid4().hex[:20].upper()}"


class WalletRechargeOrder(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "待支付"
        PAID = "paid", "已入账"
        CLOSED = "closed", "已关闭"

    class PreorderStatus(models.TextChoices):
        NOT_STARTED = "not_started", "未发起"
        SUBMITTING = "submitting", "预下单处理中"
        READY = "ready", "预下单成功"
        FAILED = "failed", "预下单失败"

    order_no = models.CharField(
        "充值单号",
        max_length=24,
        unique=True,
        default=generate_recharge_order_no,
        editable=False,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet_recharge_orders",
        verbose_name="用户",
    )
    unit_face_amount = models.PositiveBigIntegerField("单张面值（分）")
    quantity = models.PositiveSmallIntegerField("购买张数")
    credited_amount = models.PositiveBigIntegerField("入账金额（分）")
    discount_rate_bps = models.PositiveSmallIntegerField("折扣比例（万分比）")
    discount_amount = models.PositiveBigIntegerField("优惠金额（分）")
    payable_amount = models.PositiveBigIntegerField("实付金额（分）")
    pricing_snapshot = models.JSONField("充值计价快照", default=dict)
    status = models.CharField(
        "状态", max_length=24, choices=Status, default=Status.PENDING_PAYMENT
    )
    preorder_status = models.CharField(
        "汇付预下单状态",
        max_length=16,
        choices=PreorderStatus,
        default=PreorderStatus.NOT_STARTED,
    )
    gateway_merchant_id = models.CharField("汇付商户号", max_length=32, blank=True)
    req_date = models.CharField("汇付请求日期", max_length=8, blank=True)
    req_seq_id = models.CharField("汇付请求流水号", max_length=128, blank=True)
    payment_scene = models.CharField("支付场景", max_length=32, blank=True)
    trade_type = models.CharField("汇付交易类型", max_length=16, blank=True)
    gateway_trade_no = models.CharField("汇付全局流水号", max_length=128, blank=True)
    gateway_party_order_id = models.CharField("渠道商户订单号", max_length=64, blank=True)
    gateway_out_trans_id = models.CharField("渠道交易订单号", max_length=64, blank=True)
    payment_invoke_payload = models.JSONField("客户端调起参数", default=dict, blank=True)
    gateway_response_code = models.CharField("汇付响应码", max_length=32, blank=True)
    gateway_response_digest = models.CharField("汇付响应摘要", max_length=64, blank=True)
    gateway_last_query_status = models.CharField("最近查单状态", max_length=8, blank=True)
    gateway_last_query_digest = models.CharField("最近查单摘要", max_length=64, blank=True)
    gateway_last_queried_at = models.DateTimeField("最近查单时间", null=True, blank=True)
    preorder_attempts = models.PositiveSmallIntegerField("预下单尝试次数", default=0)
    preorder_requested_at = models.DateTimeField("预下单请求时间", null=True, blank=True)
    preorder_ready_at = models.DateTimeField("预下单完成时间", null=True, blank=True)
    expires_at = models.DateTimeField("支付失效时间")
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_recharge_order"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("user", "status", "-created_at"), name="wallet_recharge_user_idx"),
            models.Index(fields=("status", "expires_at"), name="wallet_recharge_exp_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(credited_amount=models.F("unit_face_amount") * models.F("quantity")),
                name="wallet_recharge_credit_matches",
            ),
            models.CheckConstraint(
                condition=Q(payable_amount=models.F("credited_amount") - models.F("discount_amount")),
                name="wallet_recharge_payable_matches",
            ),
            models.UniqueConstraint(
                fields=("req_date", "req_seq_id"),
                condition=~Q(req_seq_id=""),
                name="uniq_wallet_recharge_huifu_request",
            ),
            models.UniqueConstraint(
                fields=("gateway_trade_no",),
                condition=~Q(gateway_trade_no=""),
                name="uniq_wallet_recharge_gateway_trade",
            ),
        ]
        verbose_name = "钱包充值订单"
        verbose_name_plural = verbose_name


class WalletRechargeNotification(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received", "已接收"
        PROCESSED = "processed", "已处理"

    event_key = models.CharField("事件幂等键", max_length=64, unique=True)
    recharge_order = models.ForeignKey(
        WalletRechargeOrder,
        on_delete=models.PROTECT,
        related_name="huifu_notifications",
    )
    payload_digest = models.CharField("报文摘要", max_length=64)
    trans_stat = models.CharField("交易状态", max_length=8, blank=True)
    hf_seq_id = models.CharField("汇付全局流水号", max_length=128, blank=True)
    signature_verified = models.BooleanField(default=False)
    query_response_digest = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.RECEIVED)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "wallet_recharge_notification"
        ordering = ("-received_at", "-id")


class WalletPaymentAllocation(models.Model):
    class BusinessType(models.TextChoices):
        PROVIDER_ORDER = "provider_order", "达人订单"
        ACTIVITY_PUBLISH = "activity_publish", "活动发布"
        ACTIVITY_PARTICIPATION = "activity_participation", "活动报名"

    class Status(models.TextChoices):
        HELD = "held", "余额已冻结"
        CONSUMED = "consumed", "余额已扣款"
        RELEASED = "released", "余额已解冻"
        PARTIALLY_REFUNDED = "partially_refunded", "部分退款"
        REFUNDED = "refunded", "已退款"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet_payment_allocations",
    )
    business_type = models.CharField(max_length=32, choices=BusinessType)
    business_order_no = models.CharField(max_length=64)
    payable_amount = models.PositiveBigIntegerField("订单应付（分）")
    wallet_amount = models.PositiveBigIntegerField("余额支付（分）", default=0)
    external_amount = models.PositiveBigIntegerField("外部支付（分）", default=0)
    wallet_refunded_amount = models.PositiveBigIntegerField("余额已退（分）", default=0)
    external_refunded_amount = models.PositiveBigIntegerField("外部已退（分）", default=0)
    wallet_released = models.BooleanField("余额已解冻且未扣款", default=False)
    status = models.CharField(max_length=24, choices=Status, default=Status.HELD)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "wallet_payment_allocation"
        constraints = [
            models.UniqueConstraint(
                fields=("business_type", "business_order_no"),
                name="uniq_wallet_business_allocation",
            ),
            models.CheckConstraint(
                condition=Q(payable_amount=models.F("wallet_amount") + models.F("external_amount")),
                name="wallet_allocation_amount_matches",
            ),
            models.CheckConstraint(
                condition=Q(wallet_refunded_amount__lte=models.F("wallet_amount")),
                name="wallet_refund_lte_wallet_paid",
            ),
            models.CheckConstraint(
                condition=Q(external_refunded_amount__lte=models.F("external_amount")),
                name="external_refund_lte_external_paid",
            ),
        ]
        verbose_name = "钱包支付分配"
        verbose_name_plural = verbose_name


class WalletRefundReceipt(models.Model):
    """Idempotency receipt, including refunds with no wallet ledger movement."""

    reference_no = models.CharField(max_length=64, unique=True)
    allocation = models.ForeignKey(WalletPaymentAllocation, on_delete=models.PROTECT)
    wallet_amount = models.PositiveBigIntegerField(default=0)
    external_amount = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "wallet_refund_receipt"
