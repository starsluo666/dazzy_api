import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def generate_after_sales_case_no():
    return f"AS{uuid.uuid4().hex[:20].upper()}"


class Organization(models.Model):
    class Type(models.TextChoices):
        PLATFORM = "platform", "平台"
        REGIONAL_AGENT = "regional_agent", "区域代理"
        CITY_AGENT = "city_agent", "城市代理"

    class Status(models.TextChoices):
        ACTIVE = "active", "正常"
        DISABLED = "disabled", "停用"

    name = models.CharField("名称", max_length=80)
    code = models.SlugField("编码", max_length=50, unique=True)
    organization_type = models.CharField("类型", max_length=20, choices=Type)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children", null=True, blank=True
    )
    city_codes = models.JSONField("可管理城市编码", default=list, blank=True)
    status = models.CharField("状态", max_length=16, choices=Status, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_organization"
        ordering = ("id",)
        verbose_name = "运营组织"
        verbose_name_plural = verbose_name

    def clean(self):
        if self.parent_id and self.parent_id == self.id:
            raise ValidationError({"parent": "组织不能将自己设为上级。"})
        if self.organization_type == self.Type.PLATFORM and self.parent_id:
            raise ValidationError({"parent": "平台组织不能设置上级。"})

    def __str__(self):
        return self.name


class AdminRole(models.Model):
    class DataScope(models.TextChoices):
        ALL = "all", "全部数据"
        ORGANIZATION = "organization", "本组织"
        CITY = "city", "指定城市"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="roles", null=True, blank=True
    )
    name = models.CharField("角色名称", max_length=50)
    code = models.SlugField("角色编码", max_length=50)
    permissions = models.JSONField("权限编码", default=list, blank=True)
    data_scope = models.CharField("数据范围", max_length=20, choices=DataScope)
    is_system = models.BooleanField("系统角色", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_admin_role"
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "code"), name="uniq_backoffice_role_org_code"
            )
        ]
        verbose_name = "后台角色"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.name


class OrganizationMember(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="backoffice_memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="members"
    )
    role = models.ForeignKey(AdminRole, on_delete=models.PROTECT, related_name="members")
    city_codes = models.JSONField("额外城市范围", default=list, blank=True)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_organization_member"
        constraints = [
            models.UniqueConstraint(
                fields=("user", "organization"), name="uniq_backoffice_member_user_org"
            )
        ]
        verbose_name = "后台成员"
        verbose_name_plural = verbose_name

    def clean(self):
        if self.role_id and self.role.organization_id not in (None, self.organization_id):
            raise ValidationError({"role": "角色必须属于当前组织或为平台通用角色。"})

    def __str__(self):
        return f"{self.organization} / {self.user}"


class AdminAuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="admin_audit_logs"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="audit_logs", null=True, blank=True
    )
    action = models.CharField("操作", max_length=80)
    target_type = models.CharField("对象类型", max_length=80)
    target_id = models.CharField("对象ID", max_length=80)
    before = models.JSONField("操作前", default=dict, blank=True)
    after = models.JSONField("操作后", default=dict, blank=True)
    request_id = models.CharField("请求ID", max_length=64, blank=True)
    ip_address = models.GenericIPAddressField("IP", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "backoffice_admin_audit_log"
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("target_type", "target_id", "-created_at"))]
        verbose_name = "后台审计日志"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.actor} {self.action} {self.target_type}:{self.target_id}"


class ProviderOrderingSetting(models.Model):
    singleton_key = models.CharField(max_length=20, default="default", unique=True, editable=False)
    location_report_interval_seconds = models.PositiveSmallIntegerField(default=300)
    location_timeout_minutes = models.PositiveSmallIntegerField(default=30)
    max_location_accuracy_m = models.PositiveSmallIntegerField(default=200)
    acceptance_timeout_minutes = models.PositiveSmallIntegerField(default=30)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_provider_ordering_setting"
        verbose_name = "达人接单规则"
        verbose_name_plural = verbose_name

    @classmethod
    def current(cls):
        setting, _ = cls.objects.get_or_create(singleton_key="default")
        return setting


class PlatformOperationSetting(models.Model):
    singleton_key = models.CharField(max_length=20, default="default", unique=True, editable=False)
    provider_order_payment_timeout_minutes = models.PositiveSmallIntegerField(default=15)
    provider_order_confirmation_timeout_days = models.PositiveSmallIntegerField(default=3)
    provider_order_settlement_freeze_days = models.PositiveSmallIntegerField(default=1)
    activity_payment_timeout_minutes = models.PositiveSmallIntegerField(default=30)
    activity_service_fee_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.1000"),
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    activity_min_capacity = models.PositiveSmallIntegerField(default=2)
    activity_max_capacity = models.PositiveSmallIntegerField(default=100)
    activity_min_aa_principal_amount = models.PositiveBigIntegerField(default=1)
    activity_max_aa_principal_amount = models.PositiveBigIntegerField(default=10_000_000)
    default_activity_cover = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="platform_default_activity_cover_settings",
        null=True,
        blank=True,
    )
    activity_minimum_advance_hours = models.PositiveSmallIntegerField(default=48)
    activity_maximum_advance_days = models.PositiveSmallIntegerField(default=30)
    activity_settlement_confirmation_hours = models.PositiveSmallIntegerField(default=24)
    activity_settlement_risk_freeze_days = models.PositiveSmallIntegerField(default=7)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_platform_operation_setting"
        verbose_name = "平台运营参数"
        verbose_name_plural = verbose_name

    @classmethod
    def current(cls):
        setting, _ = cls.objects.get_or_create(singleton_key="default")
        return setting


class ProviderOrderSupportNote(models.Model):
    order = models.ForeignKey(
        "orders.ProviderOrder", on_delete=models.PROTECT, related_name="support_notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_order_support_notes",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="provider_order_support_notes",
        null=True,
        blank=True,
    )
    content = models.CharField("客服备注", max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "backoffice_provider_order_support_note"
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("order", "created_at"))]
        verbose_name = "订单客服备注"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.order.order_no} / {self.author}"


class ProviderOrderAfterSalesCase(models.Model):
    class CaseType(models.TextChoices):
        REFUND = "refund", "退款申请"
        SERVICE_DISPUTE = "service_dispute", "服务争议"
        PROVIDER_CANCEL = "provider_cancel", "达人取消"
        OTHER = "other", "其他售后"

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        PROCESSING = "processing", "处理中"
        APPROVED = "approved", "已同意·待退款"
        REFUNDED = "refunded", "退款成功"
        REJECTED = "rejected", "已驳回"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    case_no = models.CharField(
        "售后单号", max_length=24, unique=True, default=generate_after_sales_case_no,
        editable=False,
    )
    order = models.ForeignKey(
        "orders.ProviderOrder", on_delete=models.PROTECT, related_name="after_sales_cases"
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_provider_order_after_sales_cases",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="provider_order_after_sales_cases",
        null=True,
        blank=True,
    )
    case_type = models.CharField("售后类型", max_length=24, choices=CaseType)
    status = models.CharField(
        "处理状态", max_length=20, choices=Status, default=Status.PENDING
    )
    original_order_status = models.CharField("订单原状态", max_length=24)
    requested_amount = models.PositiveBigIntegerField("申请退款金额（分）", default=0)
    approved_amount = models.PositiveBigIntegerField(
        "核准退款金额（分）", null=True, blank=True
    )
    reason = models.CharField("申请原因", max_length=1000)
    evidence_object_keys = models.JSONField("凭证对象键", default=list, blank=True)
    result_note = models.CharField("审核结论", max_length=1000, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_provider_order_after_sales_cases",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_provider_order_after_sales_case"
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=("status", "-created_at")),
            models.Index(fields=("case_type", "-created_at")),
            models.Index(fields=("organization", "status", "-created_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(approved_amount__isnull=True)
                    | models.Q(approved_amount__lte=models.F("requested_amount"))
                ),
                name="after_sales_approved_lte_requested",
            ),
            models.UniqueConstraint(
                fields=("order",),
                condition=models.Q(status__in=("pending", "processing", "approved")),
                name="uniq_open_after_sales_case_per_order",
            ),
        ]
        verbose_name = "订单退款售后单"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.case_no} / {self.order.order_no}"


class UserRiskFlag(models.Model):
    class Level(models.TextChoices):
        LOW = "low", "一般关注"
        MEDIUM = "medium", "重点关注"
        HIGH = "high", "高风险"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="admin_risk_flag",
        verbose_name="用户",
    )
    level = models.CharField("风险等级", max_length=16, choices=Level, default=Level.MEDIUM)
    reason = models.CharField("标记原因", max_length=500)
    is_active = models.BooleanField("当前生效", default=True)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="marked_user_risk_flags",
        verbose_name="标记人",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="user_risk_flags",
        null=True,
        blank=True,
    )
    marked_at = models.DateTimeField("标记时间", auto_now_add=True)
    cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cleared_user_risk_flags",
        null=True,
        blank=True,
        verbose_name="解除人",
    )
    cleared_at = models.DateTimeField("解除时间", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "backoffice_user_risk_flag"
        ordering = ("-updated_at", "-id")
        verbose_name = "用户风险标记"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user} / {self.get_level_display()}"


class ProviderCreditAdjustment(models.Model):
    provider = models.ForeignKey(
        "providers.ProviderProfile",
        on_delete=models.PROTECT,
        related_name="admin_credit_adjustments",
        verbose_name="达人",
    )
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_credit_adjustments",
        verbose_name="操作人",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.PROTECT,
        related_name="provider_credit_adjustments",
        null=True,
        blank=True,
    )
    delta = models.SmallIntegerField(
        "调整分值",
        validators=(MinValueValidator(-100), MaxValueValidator(100)),
    )
    before_score = models.PositiveSmallIntegerField("调整前分值")
    after_score = models.PositiveSmallIntegerField("调整后分值")
    reason = models.CharField("调整原因", max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "backoffice_provider_credit_adjustment"
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("provider", "-created_at"))]
        verbose_name = "达人信用分调整"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.provider} {self.delta:+d}"
