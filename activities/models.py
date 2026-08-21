from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db.models import F, Q


class ActivityCategory(models.Model):
    name = models.CharField("名称", max_length=30)
    slug = models.SlugField("标识", max_length=40, unique=True)
    icon_object_key = models.CharField("图标对象键", max_length=512, blank=True)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_category"
        ordering = ("sort_order", "id")
        verbose_name = "活动分类"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.name


class Activity(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_REVIEW = "pending_review", "待审核"
        RECRUITING = "recruiting", "报名中"
        FORMED = "formed", "已成局"
        IN_PROGRESS = "in_progress", "进行中"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"
        FAILED_TO_FORM = "failed_to_form", "未成局"

    class MapSource(models.TextChoices):
        AMAP = "amap", "高德地图"

    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="organized_activities",
        verbose_name="发起人",
    )
    category = models.ForeignKey(
        ActivityCategory, on_delete=models.PROTECT, related_name="activities", verbose_name="分类"
    )
    title = models.CharField("标题", max_length=80)
    cover = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="covered_activities",
        null=True,
        blank=True,
        verbose_name="封面",
    )
    starts_at = models.DateTimeField("开始时间")
    ends_at = models.DateTimeField("结束时间")
    formation_deadline = models.DateTimeField("成局截止时间")
    meeting_place_name = models.CharField("集合地点名称", max_length=100)
    meeting_address = models.CharField("集合地点地址", max_length=255)
    map_source = models.CharField(
        "地图来源", max_length=16, choices=MapSource, default=MapSource.AMAP
    )
    source_longitude = models.DecimalField("原始GCJ-02经度", max_digits=10, decimal_places=7)
    source_latitude = models.DecimalField("原始GCJ-02纬度", max_digits=10, decimal_places=7)
    meeting_point = models.PointField("集合点（WGS84）", geography=True, srid=4326)
    capacity = models.PositiveSmallIntegerField("人数上限", validators=[MinValueValidator(2)])
    min_participants = models.PositiveSmallIntegerField(
        "最少成局人数", validators=[MinValueValidator(2)]
    )
    description = models.TextField("活动介绍")
    participation_rules = models.TextField("参与规则")
    aa_principal_amount = models.PositiveBigIntegerField(
        "单人AA本金（分）", validators=[MinValueValidator(1)]
    )
    refund_template_version = models.CharField("退款模板版本", max_length=64)
    refund_rule_snapshot = models.JSONField("退款规则快照")
    status = models.CharField("状态", max_length=20, choices=Status, default=Status.DRAFT)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity"
        indexes = [
            models.Index(fields=("status", "starts_at")),
            models.Index(fields=("category", "status", "starts_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="activity_end_after_start"
            ),
            models.CheckConstraint(
                condition=Q(formation_deadline__lt=F("starts_at")),
                name="activity_deadline_before_start",
            ),
            models.CheckConstraint(
                condition=Q(min_participants__lte=F("capacity")),
                name="activity_min_within_capacity",
            ),
        ]
        verbose_name = "活动"
        verbose_name_plural = verbose_name

    def clean(self) -> None:
        errors = {}
        if self.ends_at <= self.starts_at:
            errors["ends_at"] = "结束时间必须晚于开始时间。"
        if self.formation_deadline >= self.starts_at:
            errors["formation_deadline"] = "成局截止时间必须早于开始时间。"
        if self.min_participants > self.capacity:
            errors["min_participants"] = "最少成局人数不能大于人数上限。"
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.title


class ActivityParticipation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "已报名"
        CANCELLED = "cancelled", "已取消"

    activity = models.ForeignKey(
        Activity,
        on_delete=models.CASCADE,
        related_name="participations",
        verbose_name="活动",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_participations",
        verbose_name="参与者",
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status, default=Status.ACTIVE
    )
    joined_at = models.DateTimeField("报名时间", auto_now_add=True)
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_participation"
        constraints = [
            models.UniqueConstraint(
                fields=("activity", "user"), name="uniq_activity_participant"
            )
        ]
        indexes = [models.Index(fields=("activity", "status"))]
        verbose_name = "活动报名"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.activity} - {self.user}"


class ActivityPublishOrder(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "待支付"
        PAID = "paid", "已支付"
        CANCELLED = "cancelled", "已取消"
        REFUNDED = "refunded", "已退款"

    order_no = models.CharField("订单号", max_length=32, unique=True)
    activity = models.ForeignKey(
        Activity, on_delete=models.PROTECT, related_name="publish_orders", verbose_name="活动"
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_publish_orders",
        verbose_name="付款人",
    )
    aa_principal_amount = models.PositiveBigIntegerField("发起人AA本金（分）")
    platform_service_fee_amount = models.PositiveBigIntegerField("平台服务费（分）")
    payable_amount = models.PositiveBigIntegerField("应付金额（分）")
    pricing_snapshot = models.JSONField("计价规则快照")
    status = models.CharField(
        "状态", max_length=20, choices=Status, default=Status.PENDING_PAYMENT
    )
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_publish_order"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("payer", "status", "-created_at"))]
        verbose_name = "活动发布支付单"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.order_no
