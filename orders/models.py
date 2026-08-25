import uuid

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
    accepted_at = models.DateTimeField("达人接单时间", null=True, blank=True)
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
    customer_confirmed_at = models.DateTimeField("用户确认完成时间", null=True, blank=True)
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_order"
        indexes = [
            models.Index(fields=("customer", "status", "-created_at")),
            models.Index(fields=("provider", "starts_at", "ends_at")),
            models.Index(fields=("status", "payment_expires_at")),
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
