import uuid

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db.models import F, Q


class ActivityCategory(models.Model):
    name = models.CharField("名称", max_length=30)
    slug = models.SlugField("标识", max_length=40, unique=True)
    icon_object_key = models.CharField("图标对象键", max_length=512, blank=True)
    city_codes = models.JSONField("展示城市编码", default=list, blank=True)
    min_capacity = models.PositiveSmallIntegerField("最少人数下限", default=2)
    max_capacity = models.PositiveSmallIntegerField("人数上限", default=100)
    min_aa_principal_amount = models.PositiveBigIntegerField("最低AA本金（分）", default=1)
    max_aa_principal_amount = models.PositiveBigIntegerField(
        "最高AA本金（分）", default=10_000_000
    )
    content_guidance = models.CharField("内容规则提示", max_length=500, blank=True)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_category"
        ordering = ("sort_order", "id")
        verbose_name = "活动分类"
        verbose_name_plural = verbose_name
        constraints = [
            models.CheckConstraint(
                condition=Q(max_capacity__gte=F("min_capacity")),
                name="activity_category_capacity_range",
            ),
            models.CheckConstraint(
                condition=Q(max_aa_principal_amount__gte=F("min_aa_principal_amount")),
                name="activity_category_amount_range",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Activity(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_REVIEW = "pending_review", "待审核"
        REJECTED = "rejected", "已驳回"
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
    city_code = models.CharField("活动城市编码", max_length=20, blank=True)
    city_name = models.CharField("活动城市", max_length=50, blank=True)
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
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_activities",
        null=True,
        blank=True,
        verbose_name="审核人",
    )
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    rejection_reason = models.CharField("驳回原因", max_length=500, blank=True)
    cancellation_reason = models.CharField("取消原因", max_length=500, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cancelled_activities",
        null=True,
        blank=True,
        verbose_name="取消操作人",
    )
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
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
        PENDING_PAYMENT = "pending_payment", "待支付"
        ACTIVE = "active", "已报名"
        CANCELLED = "cancelled", "已取消"
        EXPIRED = "expired", "支付超时"

    class CancelledByRole(models.TextChoices):
        USER = "user", "参与者"
        ORGANIZER = "organizer", "发起人"
        PLATFORM = "platform", "平台"

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
        "状态", max_length=20, choices=Status, default=Status.PENDING_PAYMENT
    )
    aa_principal_amount = models.PositiveBigIntegerField("AA本金快照（分）", default=0)
    platform_service_fee_amount = models.PositiveBigIntegerField(
        "平台服务费快照（分）", default=0
    )
    payable_amount = models.PositiveBigIntegerField("应付金额快照（分）", default=0)
    pricing_snapshot = models.JSONField("计价规则快照", default=dict, blank=True)
    refund_rule_snapshot = models.JSONField("退款规则快照", default=dict, blank=True)
    rule_confirmed_at = models.DateTimeField("退款规则确认时间", null=True, blank=True)
    payment_expires_at = models.DateTimeField("支付失效时间", null=True, blank=True)
    joined_at = models.DateTimeField("报名时间", null=True, blank=True)
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    cancellation_reason = models.CharField("取消原因", max_length=500, blank=True)
    cancelled_by_role = models.CharField(
        "取消方", max_length=16, choices=CancelledByRole, blank=True
    )
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


def generate_participation_order_no():
    return f"APO{uuid.uuid4().hex[:20].upper()}"


class ActivityParticipationPaymentOrder(models.Model):
    class Channel(models.TextChoices):
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

    order_no = models.CharField(
        "支付单号", max_length=24, unique=True, default=generate_participation_order_no,
        editable=False,
    )
    participation = models.ForeignKey(
        ActivityParticipation,
        on_delete=models.PROTECT,
        related_name="payment_orders",
        verbose_name="活动报名",
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_participation_payment_orders",
        verbose_name="付款人",
    )
    aa_principal_amount = models.PositiveBigIntegerField("AA本金（分）")
    platform_service_fee_amount = models.PositiveBigIntegerField("平台服务费（分）")
    payable_amount = models.PositiveBigIntegerField("应付金额（分）")
    pricing_snapshot = models.JSONField("计价规则快照")
    channel = models.CharField("支付渠道", max_length=20, choices=Channel)
    status = models.CharField(
        "支付状态", max_length=24, choices=Status, default=Status.PENDING_PAYMENT
    )
    gateway_trade_no = models.CharField("渠道交易号", max_length=64, blank=True)
    expires_at = models.DateTimeField("支付失效时间")
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_participation_payment_order"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("payer", "status", "-created_at"),
                name="activity_pa_payer_i_67dc93_idx",
            ),
            models.Index(
                fields=("expires_at", "status"),
                name="activity_pa_expires_9a6487_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    payable_amount=F("aa_principal_amount")
                    + F("platform_service_fee_amount")
                ),
                name="activity_participation_payment_amount_matches",
            ),
            models.UniqueConstraint(
                fields=("participation",),
                condition=Q(status="pending_payment"),
                name="uniq_pending_payment_per_activity_participation",
            ),
        ]
        verbose_name = "活动报名支付单"
        verbose_name_plural = verbose_name


def generate_participation_refund_no():
    return f"APR{uuid.uuid4().hex[:20].upper()}"


class ActivityParticipationRefundOrder(models.Model):
    class RefundType(models.TextChoices):
        PARTICIPANT_CANCELLATION = "participant_cancellation", "参与者取消"
        ORGANIZER_CANCELLATION = "organizer_cancellation", "发起人取消"
        ADMIN_CANCELLATION = "admin_cancellation", "后台取消"
        FAILED_TO_FORM = "failed_to_form", "未成局退款"
        AFTER_SALES = "after_sales", "售后退款"
        PAYMENT_TIMEOUT = "payment_timeout", "支付超时异常退款"

    class Status(models.TextChoices):
        PENDING = "pending", "待退款"
        PROCESSING = "processing", "退款处理中"
        SUCCEEDED = "succeeded", "退款成功"
        FAILED = "failed", "退款失败"

    class PrincipalDestination(models.TextChoices):
        NONE = "none", "无扣除"
        ORGANIZER = "organizer", "归发起人"
        PLATFORM = "platform", "归平台"

    refund_no = models.CharField(
        "退款单号", max_length=24, unique=True, default=generate_participation_refund_no,
        editable=False,
    )
    idempotency_key = models.CharField("幂等键", max_length=96, unique=True)
    activity = models.ForeignKey(
        Activity,
        on_delete=models.PROTECT,
        related_name="participation_refund_orders",
        verbose_name="活动",
    )
    participation = models.ForeignKey(
        ActivityParticipation,
        on_delete=models.PROTECT,
        related_name="refund_orders",
        verbose_name="活动报名",
    )
    payment_order = models.ForeignKey(
        ActivityParticipationPaymentOrder,
        on_delete=models.PROTECT,
        related_name="refund_orders",
        verbose_name="原支付单",
    )
    beneficiary = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_participation_refund_orders",
        verbose_name="退款用户",
    )
    refund_type = models.CharField("退款类型", max_length=32, choices=RefundType)
    principal_refund_amount = models.PositiveBigIntegerField("AA本金退款（分）")
    service_fee_refund_amount = models.PositiveBigIntegerField("平台服务费退款（分）")
    refund_amount = models.PositiveBigIntegerField("退款总额（分）")
    retained_principal_amount = models.PositiveBigIntegerField("扣除AA本金（分）", default=0)
    retained_service_fee_amount = models.PositiveBigIntegerField(
        "扣除平台服务费（分）", default=0
    )
    retained_principal_destination = models.CharField(
        "扣除本金归属",
        max_length=16,
        choices=PrincipalDestination,
        default=PrincipalDestination.NONE,
    )
    status = models.CharField(
        "退款状态", max_length=20, choices=Status, default=Status.PENDING
    )
    gateway_refund_no = models.CharField("渠道退款号", max_length=64, blank=True)
    reason = models.CharField("退款原因", max_length=1000)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="operated_activity_participation_refunds",
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
        db_table = "activity_participation_refund_order"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("activity", "status", "-created_at"),
                name="activity_pa_activit_54c052_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    refund_amount=F("principal_refund_amount")
                    + F("service_fee_refund_amount")
                ),
                name="activity_participation_refund_amount_matches",
            )
        ]
        verbose_name = "活动报名退款单"
        verbose_name_plural = verbose_name


def generate_activity_after_sales_case_no():
    return f"AAS{uuid.uuid4().hex[:20].upper()}"


class ActivityAfterSalesCase(models.Model):
    class Reason(models.TextChoices):
        FORCE_MAJEURE = "force_majeure", "不可抗力"
        ILLNESS_OR_ACCIDENT = "illness_or_accident", "疾病或事故"
        FALSE_INFORMATION = "false_information", "活动信息不实"
        CONTENT_MISMATCH = "content_mismatch", "活动内容不符"
        VENUE_CHANGE = "venue_change", "临时变更场地"
        NOT_FULFILLED = "not_fulfilled", "活动未履约"
        OTHER = "other", "其他问题"

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        PROCESSING = "processing", "处理中"
        APPROVED = "approved", "已同意"
        REJECTED = "rejected", "已驳回"

    case_no = models.CharField(
        "售后单号", max_length=24, unique=True,
        default=generate_activity_after_sales_case_no, editable=False,
    )
    participation = models.ForeignKey(
        ActivityParticipation,
        on_delete=models.PROTECT,
        related_name="after_sales_cases",
        verbose_name="活动报名",
    )
    applicant = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_after_sales_cases",
        verbose_name="申请人",
    )
    reason = models.CharField("售后原因", max_length=32, choices=Reason)
    description = models.CharField("问题说明", max_length=1000)
    evidence_object_keys = models.JSONField("证明材料对象键", default=list, blank=True)
    requested_principal_amount = models.PositiveBigIntegerField("申请AA本金退款（分）")
    requested_service_fee_amount = models.PositiveBigIntegerField(
        "申请平台服务费退款（分）"
    )
    requested_amount = models.PositiveBigIntegerField("申请退款总额（分）")
    approved_principal_amount = models.PositiveBigIntegerField(
        "核准AA本金退款（分）", null=True, blank=True
    )
    approved_service_fee_amount = models.PositiveBigIntegerField(
        "核准平台服务费退款（分）", null=True, blank=True
    )
    approved_amount = models.PositiveBigIntegerField(
        "核准退款总额（分）", null=True, blank=True
    )
    status = models.CharField(
        "处理状态", max_length=20, choices=Status, default=Status.PENDING
    )
    refund_order = models.OneToOneField(
        ActivityParticipationRefundOrder,
        on_delete=models.PROTECT,
        related_name="after_sales_case",
        null=True,
        blank=True,
        verbose_name="退款单",
    )
    result_note = models.CharField("处理结论", max_length=1000, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_activity_after_sales_cases",
        null=True,
        blank=True,
        verbose_name="处理人",
    )
    reviewed_at = models.DateTimeField("处理时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_after_sales_case"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("status", "-created_at"),
                name="activity_af_status_75a497_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    requested_amount=F("requested_principal_amount")
                    + F("requested_service_fee_amount")
                ),
                name="activity_after_sales_requested_amount_matches",
            ),
            models.UniqueConstraint(
                fields=("participation",),
                condition=Q(status__in=("pending", "processing")),
                name="uniq_open_activity_after_sales_per_participation",
            ),
        ]
        verbose_name = "活动退款售后单"
        verbose_name_plural = verbose_name


class ActivityPublishOrder(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "待支付"
        PAID = "paid", "已支付"
        CANCELLED = "cancelled", "已取消"
        PARTIALLY_REFUNDED = "partially_refunded", "部分退款"
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
    expires_at = models.DateTimeField("支付失效时间")
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    closed_at = models.DateTimeField("关闭时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_publish_order"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("payer", "status", "-created_at")),
            models.Index(
                fields=("status", "expires_at"),
                name="activity_pub_status_exp_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("activity", "payer"),
                condition=Q(status="pending_payment"),
                name="uniq_pending_activity_publish_payment",
            )
        ]
        verbose_name = "活动发布支付单"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.order_no


def generate_activity_refund_no():
    return f"ARF{uuid.uuid4().hex[:20].upper()}"


class ActivityRefundRecord(models.Model):
    class RefundType(models.TextChoices):
        REVIEW_REJECTION = "review_rejection", "审核驳回退款"
        ADMIN_CANCELLATION = "admin_cancellation", "后台取消退款"
        ORGANIZER_CANCELLATION = "organizer_cancellation", "发起人取消退款"
        FAILED_TO_FORM = "failed_to_form", "未成局退款"

    class Status(models.TextChoices):
        SIMULATED_REFUNDED = "simulated_refunded", "模拟退款成功"

    refund_no = models.CharField(
        "退款单号", max_length=24, unique=True, default=generate_activity_refund_no,
        editable=False,
    )
    activity = models.ForeignKey(
        Activity, on_delete=models.PROTECT, related_name="refund_records", verbose_name="活动"
    )
    publish_order = models.OneToOneField(
        ActivityPublishOrder,
        on_delete=models.PROTECT,
        related_name="refund_record",
        verbose_name="发布支付单",
    )
    beneficiary = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_refund_records",
        verbose_name="退款用户",
    )
    refund_type = models.CharField("退款类型", max_length=24, choices=RefundType)
    principal_amount = models.PositiveBigIntegerField("AA本金退款（分）")
    service_fee_amount = models.PositiveBigIntegerField("平台服务费退款（分）")
    refund_amount = models.PositiveBigIntegerField("退款总额（分）")
    retained_principal_amount = models.PositiveBigIntegerField(
        "扣除发起人AA本金（分）", default=0
    )
    retained_service_fee_amount = models.PositiveBigIntegerField(
        "扣除发起人平台服务费（分）", default=0
    )
    retained_principal_destination = models.CharField(
        "扣除本金归属", max_length=16, blank=True
    )
    status = models.CharField(
        "退款状态", max_length=24, choices=Status, default=Status.SIMULATED_REFUNDED
    )
    reason = models.CharField("退款原因", max_length=500)
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="operated_activity_refunds",
        null=True,
        blank=True,
        verbose_name="操作人",
    )
    refunded_at = models.DateTimeField("退款时间")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "activity_refund_record"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("activity", "-created_at"),
                name="activity_re_activit_2aae89_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(refund_amount=F("principal_amount") + F("service_fee_amount")),
                name="activity_refund_amount_matches_components",
            )
        ]
        verbose_name = "活动退款记录"
        verbose_name_plural = verbose_name


def generate_activity_settlement_no():
    return f"AST{uuid.uuid4().hex[:20].upper()}"


class ActivitySettlement(models.Model):
    class Status(models.TextChoices):
        CONFIRMING = "confirming", "履约确认中"
        RISK_FROZEN = "risk_frozen", "风险冻结中"
        DISPUTE_FROZEN = "dispute_frozen", "争议冻结中"
        SETTLED = "settled", "已结算入账"

    class DisputeSource(models.TextChoices):
        NONE = "", "无"
        AFTER_SALES = "after_sales", "退款售后"
        ADMIN = "admin", "后台风控"

    settlement_no = models.CharField(
        "结算单号", max_length=24, unique=True,
        default=generate_activity_settlement_no, editable=False,
    )
    activity = models.OneToOneField(
        Activity, on_delete=models.PROTECT, related_name="settlement", verbose_name="活动"
    )
    beneficiary = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_settlements",
        verbose_name="结算受益人",
    )
    organizer_principal_amount = models.PositiveBigIntegerField("发起人AA本金（分）")
    participant_principal_amount = models.PositiveBigIntegerField(
        "有效参与者AA本金（分）"
    )
    retained_participant_principal_amount = models.PositiveBigIntegerField(
        "取消参与者归发起人本金（分）"
    )
    settlement_amount = models.PositiveBigIntegerField("发起人结算金额（分）")
    platform_service_fee_amount = models.PositiveBigIntegerField(
        "平台组局服务费净额（分）"
    )
    status = models.CharField(
        "结算状态", max_length=24, choices=Status, default=Status.CONFIRMING
    )
    confirmation_started_at = models.DateTimeField("履约确认开始时间")
    confirmation_deadline = models.DateTimeField("履约确认截止时间")
    risk_frozen_at = models.DateTimeField("风险冻结开始时间", null=True, blank=True)
    freeze_until = models.DateTimeField("风险冻结截止时间")
    dispute_reason = models.CharField("争议冻结原因", max_length=1000, blank=True)
    dispute_source = models.CharField(
        "争议来源", max_length=20, choices=DisputeSource, blank=True
    )
    calculation_snapshot = models.JSONField("结算计算快照", default=dict)
    settled_at = models.DateTimeField("结算入账时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_settlement"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("status", "freeze_until"),
                name="activity_se_status_6466a2_idx",
            ),
            models.Index(
                fields=("beneficiary", "status", "-created_at"),
                name="activity_se_benefic_6c3f1a_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    settlement_amount=F("organizer_principal_amount")
                    + F("participant_principal_amount")
                    + F("retained_participant_principal_amount")
                ),
                name="activity_settlement_amount_matches_components",
            ),
            models.CheckConstraint(
                condition=Q(freeze_until__gte=F("confirmation_deadline")),
                name="activity_settlement_freeze_after_confirmation",
            ),
        ]
        verbose_name = "活动结算单"
        verbose_name_plural = verbose_name


def generate_activity_report_no():
    return f"ARP{uuid.uuid4().hex[:20].upper()}"


class ActivityReport(models.Model):
    class Reason(models.TextChoices):
        FALSE_INFORMATION = "false_information", "信息不实"
        INAPPROPRIATE_CONTENT = "inappropriate_content", "内容不当"
        PRIVATE_TRANSACTION = "private_transaction", "诱导私下交易"
        SAFETY_RISK = "safety_risk", "存在安全风险"
        OTHER = "other", "其他问题"

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        PROCESSING = "processing", "处理中"
        RESOLVED = "resolved", "已处理"
        REJECTED = "rejected", "不予受理"

    case_no = models.CharField(
        "举报单号", max_length=24, unique=True, default=generate_activity_report_no,
        editable=False,
    )
    activity = models.ForeignKey(
        Activity, on_delete=models.PROTECT, related_name="reports", verbose_name="活动"
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="activity_reports",
        verbose_name="举报人",
    )
    reason = models.CharField("举报原因", max_length=32, choices=Reason)
    description = models.CharField("补充说明", max_length=1000, blank=True)
    status = models.CharField(
        "处理状态", max_length=20, choices=Status, default=Status.PENDING
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_activity_reports",
        null=True,
        blank=True,
        verbose_name="处理人",
    )
    result_note = models.CharField("处理结论", max_length=1000, blank=True)
    reviewed_at = models.DateTimeField("处理时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "activity_report"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("status", "-created_at"), name="activity_re_status_bfcc83_idx"
            ),
            models.Index(
                fields=("activity", "status", "-created_at"),
                name="activity_re_activit_823ea1_idx",
            ),
        ]
        verbose_name = "活动举报"
        verbose_name_plural = verbose_name
