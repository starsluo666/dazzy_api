import uuid
from decimal import Decimal

from django.conf import settings
from django.contrib.gis.db import models
from django.db.models import Q

from providers.models import ProviderProfile, ProviderService


class ProviderOrder(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "待支付"
        PENDING_ACCEPTANCE = "pending_acceptance", "待接单"
        PENDING_SUPPORT = "pending_support", "待客服处理"
        PENDING_SERVICE = "pending_service", "待服务"
        DEPARTED = "departed", "已出发"
        IN_SERVICE = "in_service", "服务中"
        PENDING_CONFIRMATION = "pending_confirmation", "待确认"
        PENDING_REVIEW = "pending_review", "待评价"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"
        AFTER_SALES = "after_sales", "售后中"
        REFUNDED = "refunded", "已退款"

    class BillingType(models.TextChoices):
        HOURLY = "hourly", "按小时"
        PER_SESSION = "per_session", "按次"

    class ContactGender(models.TextChoices):
        MR = "mr", "先生"
        MS = "ms", "女士"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    order_no = models.CharField("订单号", max_length=32, unique=True)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="provider_orders"
    )
    provider = models.ForeignKey(
        ProviderProfile, on_delete=models.PROTECT, related_name="orders"
    )
    service = models.ForeignKey(ProviderService, on_delete=models.PROTECT, related_name="orders")
    provider_name_snapshot = models.CharField("达人名称快照", max_length=30)
    service_name_snapshot = models.CharField("服务名称快照", max_length=30)
    billing_type_snapshot = models.CharField(
        "计费方式快照", max_length=16, choices=BillingType
    )
    unit_price_amount = models.PositiveBigIntegerField("单价快照（分）")
    starts_at = models.DateTimeField("服务开始时间")
    ends_at = models.DateTimeField("服务结束时间")
    duration_minutes = models.PositiveIntegerField("服务时长（分钟）")
    map_source = models.CharField("地图来源", max_length=16, default="amap")
    meeting_location_name = models.CharField(
        "集合地点名称快照", max_length=100, blank=True, default=""
    )
    meeting_address = models.CharField("集合地点", max_length=255)
    source_longitude = models.DecimalField(
        "原始GCJ-02经度", max_digits=10, decimal_places=7, null=True, blank=True
    )
    source_latitude = models.DecimalField(
        "原始GCJ-02纬度", max_digits=10, decimal_places=7, null=True, blank=True
    )
    route_distance_km = models.DecimalField(
        "单程路线距离（公里）", max_digits=7, decimal_places=2, null=True, blank=True
    )
    contact_name = models.CharField("联系人", max_length=30)
    contact_gender = models.CharField(
        "联系人称谓", max_length=8, choices=ContactGender, blank=True, default=""
    )
    contact_phone = models.CharField("联系电话", max_length=20)
    note = models.CharField("备注", max_length=500, blank=True)
    service_fee_amount = models.PositiveBigIntegerField("服务费（分）")
    transport_fee_amount = models.PositiveBigIntegerField("往返交通费（分）", default=0)
    other_fee_amount = models.PositiveBigIntegerField("其他费用（分）", default=0)
    discount_amount = models.PositiveBigIntegerField("优惠金额（分）", default=0)
    payable_amount = models.PositiveBigIntegerField("应付金额（分）")
    pricing_snapshot = models.JSONField("计价规则快照", default=dict)
    status = models.CharField(
        "订单状态", max_length=24, choices=Status, default=Status.PENDING_PAYMENT
    )
    payment_expires_at = models.DateTimeField("支付及档期锁定截止时间")
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    acceptance_expires_at = models.DateTimeField("达人接单截止时间", null=True, blank=True)
    accepted_at = models.DateTimeField("达人接单时间", null=True, blank=True)
    provider_rejected_at = models.DateTimeField("达人拒单时间", null=True, blank=True)
    provider_rejection_reason = models.CharField("达人拒单原因", max_length=200, blank=True)
    departed_at = models.DateTimeField("达人出发时间", null=True, blank=True)
    arrival_photo = models.OneToOneField(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="provider_order_arrival_evidence",
        null=True,
        blank=True,
        limit_choices_to={"category": "order_evidence"},
        verbose_name="集合地点照片",
    )
    arrival_photo_uploaded_at = models.DateTimeField("集合照绑定时间", null=True, blank=True)
    arrival_longitude = models.DecimalField(
        "集合照GCJ-02经度", max_digits=10, decimal_places=7, null=True, blank=True
    )
    arrival_latitude = models.DecimalField(
        "集合照GCJ-02纬度", max_digits=10, decimal_places=7, null=True, blank=True
    )
    arrival_location_accuracy_m = models.DecimalField(
        "集合照定位精度（米）", max_digits=8, decimal_places=2, null=True, blank=True
    )
    service_started_at = models.DateTimeField("服务开始时间", null=True, blank=True)
    completion_submitted_at = models.DateTimeField("达人提交完成时间", null=True, blank=True)
    confirmation_expires_at = models.DateTimeField("用户确认截止时间", null=True, blank=True)
    customer_confirmed_at = models.DateTimeField("用户确认完成时间", null=True, blank=True)
    auto_confirmed_at = models.DateTimeField("系统自动确认时间", null=True, blank=True)
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_order"
        indexes = [
            models.Index(fields=("customer", "status", "-created_at")),
            models.Index(fields=("provider", "starts_at", "ends_at")),
            models.Index(fields=("status", "payment_expires_at")),
            models.Index(fields=("status", "confirmation_expires_at")),
            models.Index(fields=("created_at",), name="provider_order_created_idx"),
            models.Index(
                fields=("paid_at",),
                condition=Q(paid_at__isnull=False),
                name="provider_order_paid_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=models.F("starts_at")), name="order_end_after_start"),
            models.CheckConstraint(condition=Q(payable_amount__gte=0), name="order_payable_nonnegative"),
            models.CheckConstraint(
                condition=Q(contact_gender__in=("", "mr", "ms")),
                name="provider_order_contact_gender_valid",
            ),
        ]
        ordering = ("-created_at",)
        verbose_name = "达人订单"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.order_no


def generate_provider_payment_no():
    return f"POP{uuid.uuid4().hex[:20].upper()}"


class ProviderOrderPaymentOrder(models.Model):
    class Channel(models.TextChoices):
        UNSELECTED = "unselected", "待选择"
        MOCK_WECHAT = "mock_wechat", "模拟微信支付"
        MOCK_ALIPAY = "mock_alipay", "模拟支付宝"
        WECHAT = "wechat", "微信支付"
        ALIPAY = "alipay", "支付宝"

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "待支付"
        PAID = "paid", "已支付"
        CLOSED = "closed", "已关闭"
        PARTIALLY_REFUNDED = "partially_refunded", "部分退款"
        REFUNDED = "refunded", "已退款"

    payment_no = models.CharField(
        "支付单号",
        max_length=24,
        unique=True,
        default=generate_provider_payment_no,
        editable=False,
    )
    order = models.OneToOneField(
        ProviderOrder,
        on_delete=models.PROTECT,
        related_name="payment_order",
        verbose_name="达人订单",
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_order_payment_orders",
        verbose_name="付款人",
    )
    service_fee_amount = models.PositiveBigIntegerField("服务费（分）")
    transport_fee_amount = models.PositiveBigIntegerField("交通费（分）")
    other_fee_amount = models.PositiveBigIntegerField("其他费用（分）")
    discount_amount = models.PositiveBigIntegerField("优惠金额（分）")
    payable_amount = models.PositiveBigIntegerField("应付金额（分）")
    pricing_snapshot = models.JSONField("计价规则快照", default=dict)
    channel = models.CharField(
        "支付渠道",
        max_length=20,
        choices=Channel,
        default=Channel.UNSELECTED,
    )
    status = models.CharField(
        "支付状态",
        max_length=24,
        choices=Status,
        default=Status.PENDING_PAYMENT,
    )
    gateway_trade_no = models.CharField("渠道交易号", max_length=64, blank=True)
    expires_at = models.DateTimeField("支付失效时间")
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_order_payment_order"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("payer", "status", "-created_at"),
                name="provider_pay_payer_status_idx",
            ),
            models.Index(
                fields=("status", "expires_at"),
                name="provider_pay_status_exp_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    payable_amount=(
                        models.F("service_fee_amount")
                        + models.F("transport_fee_amount")
                        + models.F("other_fee_amount")
                        - models.F("discount_amount")
                    )
                ),
                name="provider_payment_amount_matches",
            ),
            models.UniqueConstraint(
                fields=("gateway_trade_no",),
                condition=~Q(gateway_trade_no=""),
                name="uniq_provider_gateway_trade_no",
            ),
        ]
        verbose_name = "达人订单支付单"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.payment_no} / {self.order.order_no}"


def generate_provider_refund_no():
    return f"POR{uuid.uuid4().hex[:20].upper()}"


class ProviderOrderRefundOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待退款"
        PROCESSING = "processing", "退款处理中"
        SUCCEEDED = "succeeded", "退款成功"
        FAILED = "failed", "退款失败"

    class SourceType(models.TextChoices):
        AFTER_SALES = "after_sales", "退款售后"
        ADMIN = "admin", "后台退款"
        SYSTEM = "system", "系统退款"

    refund_no = models.CharField(
        "退款单号",
        max_length=24,
        unique=True,
        default=generate_provider_refund_no,
        editable=False,
    )
    idempotency_key = models.CharField("幂等键", max_length=120, unique=True)
    order = models.ForeignKey(
        ProviderOrder,
        on_delete=models.PROTECT,
        related_name="refund_orders",
        verbose_name="达人订单",
    )
    payment_order = models.ForeignKey(
        ProviderOrderPaymentOrder,
        on_delete=models.PROTECT,
        related_name="refund_orders",
        verbose_name="原支付单",
    )
    beneficiary = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_order_refund_orders",
        verbose_name="退款用户",
    )
    source_type = models.CharField("退款来源", max_length=20, choices=SourceType)
    source_reference = models.CharField("来源单号", max_length=64)
    service_fee_refund_amount = models.PositiveBigIntegerField("服务费退款（分）")
    transport_fee_refund_amount = models.PositiveBigIntegerField("交通费退款（分）")
    other_fee_refund_amount = models.PositiveBigIntegerField("其他费用退款（分）")
    refund_amount = models.PositiveBigIntegerField("退款总额（分）")
    allocation_snapshot = models.JSONField("退款分配快照", default=dict)
    status = models.CharField(
        "退款状态", max_length=20, choices=Status, default=Status.PENDING
    )
    gateway_refund_no = models.CharField("渠道退款号", max_length=64, blank=True)
    reason = models.CharField("退款原因", max_length=1000)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="operated_provider_order_refunds",
        null=True,
        blank=True,
        verbose_name="操作人",
    )
    requested_at = models.DateTimeField("申请时间", auto_now_add=True)
    refunded_at = models.DateTimeField("退款完成时间", null=True, blank=True)
    failure_reason = models.CharField("失败原因", max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_order_refund_order"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("order", "status", "-created_at"),
                name="provider_ref_order_status_idx",
            ),
            models.Index(
                fields=("status", "requested_at"),
                name="provider_ref_status_req_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    refund_amount=(
                        models.F("service_fee_refund_amount")
                        + models.F("transport_fee_refund_amount")
                        + models.F("other_fee_refund_amount")
                    )
                ),
                name="provider_refund_amount_matches",
            ),
            models.CheckConstraint(
                condition=Q(refund_amount__gt=0),
                name="provider_refund_amount_positive",
            ),
            models.UniqueConstraint(
                fields=("gateway_refund_no",),
                condition=~Q(gateway_refund_no=""),
                name="uniq_provider_gateway_refund_no",
            ),
        ]
        verbose_name = "达人订单退款单"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.refund_no} / {self.order.order_no}"


def generate_provider_settlement_no():
    return f"POS{uuid.uuid4().hex[:20].upper()}"


class ProviderOrderSettlement(models.Model):
    class Status(models.TextChoices):
        RISK_FROZEN = "risk_frozen", "风险冻结中"
        DISPUTE_FROZEN = "dispute_frozen", "争议冻结中"
        SETTLED = "settled", "已结算入账"
        CANCELLED = "cancelled", "已取消"

    settlement_no = models.CharField(
        "结算单号",
        max_length=24,
        unique=True,
        default=generate_provider_settlement_no,
        editable=False,
    )
    order = models.OneToOneField(
        ProviderOrder,
        on_delete=models.PROTECT,
        related_name="settlement",
        verbose_name="达人订单",
    )
    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.PROTECT,
        related_name="order_settlements",
        verbose_name="结算达人",
    )
    paid_amount = models.PositiveBigIntegerField("实付金额（分）")
    refunded_amount = models.PositiveBigIntegerField("已退款金额（分）", default=0)
    net_service_fee_amount = models.PositiveBigIntegerField("净服务费（分）")
    net_transport_fee_amount = models.PositiveBigIntegerField("净交通费（分）")
    net_other_fee_amount = models.PositiveBigIntegerField("净其他费用（分）")
    platform_commission_rate = models.DecimalField(
        "平台抽成比例（%）", max_digits=5, decimal_places=2, default=Decimal("20.00")
    )
    platform_commission_amount = models.PositiveBigIntegerField("平台抽成（分）")
    provider_service_income_amount = models.PositiveBigIntegerField("达人服务收入（分）")
    provider_settlement_amount = models.PositiveBigIntegerField("达人结算金额（分）")
    status = models.CharField(
        "结算状态", max_length=24, choices=Status, default=Status.RISK_FROZEN
    )
    frozen_at = models.DateTimeField("冻结开始时间")
    freeze_until = models.DateTimeField("冻结截止时间")
    dispute_reason = models.CharField("争议冻结原因", max_length=1000, blank=True)
    calculation_snapshot = models.JSONField("结算计算快照", default=dict)
    settled_at = models.DateTimeField("结算入账时间", null=True, blank=True)
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_order_settlement"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("status", "freeze_until"),
                name="provider_set_status_freeze_idx",
            ),
            models.Index(
                fields=("provider", "status", "-created_at"),
                name="provider_set_owner_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(refunded_amount__lte=models.F("paid_amount")),
                name="provider_set_refunded_lte_paid",
            ),
            models.CheckConstraint(
                condition=Q(
                    provider_settlement_amount=(
                        models.F("provider_service_income_amount")
                        + models.F("net_transport_fee_amount")
                        + models.F("net_other_fee_amount")
                    )
                ),
                name="provider_settlement_amount_matches",
            ),
            models.CheckConstraint(
                condition=Q(
                    paid_amount=(
                        models.F("refunded_amount")
                        + models.F("platform_commission_amount")
                        + models.F("provider_settlement_amount")
                    )
                ),
                name="provider_settlement_balance_matches",
            ),
            models.CheckConstraint(
                condition=Q(freeze_until__gte=models.F("frozen_at")),
                name="provider_settlement_freeze_valid",
            ),
        ]
        verbose_name = "达人订单结算单"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.settlement_no} / {self.order.order_no}"

class ProviderOrderReview(models.Model):
    order = models.OneToOneField(
        ProviderOrder,
        on_delete=models.PROTECT,
        related_name="review",
        verbose_name="达人订单",
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_order_reviews",
        verbose_name="评价用户",
    )
    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.PROTECT,
        related_name="order_reviews",
        verbose_name="达人",
    )
    rating = models.PositiveSmallIntegerField("评分")
    content = models.CharField("评价内容", max_length=500, blank=True)
    images = models.ManyToManyField(
        "mediafiles.MediaAsset",
        related_name="provider_order_reviews",
        blank=True,
        verbose_name="评价图片",
    )
    is_anonymous = models.BooleanField("匿名评价", default=False)
    is_visible = models.BooleanField("公开显示", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_order_review"
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(rating__gte=1) & models.Q(rating__lte=5),
                name="provider_review_rating_between_1_5",
            )
        ]
        indexes = [
            models.Index(fields=("provider", "is_visible", "-created_at")),
        ]
        verbose_name = "达人订单评价"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.order.order_no} / {self.rating}星"
