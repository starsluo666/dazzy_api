from decimal import Decimal

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator


class ServiceCategory(models.Model):
    name = models.CharField("名称", max_length=30)
    slug = models.SlugField("标识", max_length=40, unique=True)
    icon_object_key = models.CharField("图标对象键", max_length=512, blank=True)
    city_codes = models.JSONField("展示城市编码", default=list, blank=True)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)
    platform_commission_rate = models.DecimalField(
        "平台抽成比例（%）",
        max_digits=5,
        decimal_places=2,
        default=Decimal("20.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
    )
    hourly_min_price_amount = models.PositiveBigIntegerField(
        "按小时最低价格（分）", default=1, validators=[MinValueValidator(1)]
    )
    hourly_max_price_amount = models.PositiveBigIntegerField(
        "按小时最高价格（分）", default=10_000_000, validators=[MinValueValidator(1)]
    )
    per_session_min_price_amount = models.PositiveBigIntegerField(
        "按次最低价格（分）", default=1, validators=[MinValueValidator(1)]
    )
    per_session_max_price_amount = models.PositiveBigIntegerField(
        "按次最高价格（分）", default=10_000_000, validators=[MinValueValidator(1)]
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_service_category"
        ordering = ("sort_order", "id")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(hourly_min_price_amount__lte=models.F("hourly_max_price_amount")),
                name="provider_category_hourly_price_range",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    per_session_min_price_amount__lte=models.F("per_session_max_price_amount")
                ),
                name="provider_category_session_price_range",
            ),
        ]
        verbose_name = "达人服务分类"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.name

    def price_range_for(self, billing_type: str) -> tuple[int, int]:
        if billing_type == "hourly":
            return self.hourly_min_price_amount, self.hourly_max_price_amount
        return self.per_session_min_price_amount, self.per_session_max_price_amount


class ProviderProfile(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已驳回"
        SUSPENDED = "suspended", "已暂停"

    class IdentityStatus(models.TextChoices):
        UNVERIFIED = "unverified", "未认证"
        PENDING = "pending", "认证中"
        VERIFIED = "verified", "已认证"
        REJECTED = "rejected", "认证未通过"

    class OnboardingStatus(models.TextChoices):
        INCOMPLETE = "incomplete", "待完善"
        PENDING_REVIEW = "pending_review", "待开通审核"
        APPROVED = "approved", "已开通"
        REJECTED = "rejected", "开通审核未通过"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_profile",
        verbose_name="用户",
    )
    status = models.CharField("审核状态", max_length=16, choices=Status, default=Status.DRAFT)
    application_real_name = models.CharField("申请真实姓名", max_length=50, blank=True)
    application_birth_date = models.DateField("申请出生日期", null=True, blank=True)
    display_name = models.CharField("达人名称", max_length=30, blank=True)
    bio = models.TextField("个人简介", blank=True)
    lifestyle_photo = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="provider_lifestyle_profiles",
        null=True,
        blank=True,
        verbose_name="生活照",
    )
    service_city_code = models.CharField("服务城市编码", max_length=20, blank=True)
    service_city_name = models.CharField("服务城市", max_length=50, blank=True)
    max_service_radius_km = models.PositiveSmallIntegerField(
        "最大服务半径（公里）",
        default=10,
        validators=[MinValueValidator(10), MaxValueValidator(70)],
    )
    rating = models.DecimalField(
        "评分",
        max_digits=3,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    service_count = models.PositiveIntegerField("服务次数", default=0)
    commission_reset_period_override = models.CharField(max_length=16, blank=True, default="")
    commission_tiers_override = models.JSONField(null=True, blank=True, default=None)
    order_count = models.PositiveIntegerField("接单量", default=0)
    credit_score = models.PositiveSmallIntegerField("信用分", default=100)
    invitation_code = models.CharField("邀请码", max_length=32, blank=True)
    agreement_accepted_at = models.DateTimeField("协议同意时间", null=True, blank=True)
    submitted_at = models.DateTimeField("申请提交时间", null=True, blank=True)
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    rejection_reason = models.CharField("驳回原因", max_length=500, blank=True)
    identity_status = models.CharField(
        "达人实名认证状态",
        max_length=16,
        choices=IdentityStatus,
        default=IdentityStatus.UNVERIFIED,
    )
    identity_real_name = models.CharField("实名姓名", max_length=50, blank=True)
    identity_number_masked = models.CharField("证件号码脱敏值", max_length=32, blank=True)
    identity_number_digest = models.CharField("证件号码摘要", max_length=64, blank=True)
    identity_front_photo = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="provider_identity_front_profiles",
        null=True,
        blank=True,
        verbose_name="身份证人像面",
    )
    identity_back_photo = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="provider_identity_back_profiles",
        null=True,
        blank=True,
        verbose_name="身份证国徽面",
    )
    identity_face_photo = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="provider_identity_face_profiles",
        null=True,
        blank=True,
        verbose_name="本人核验照片",
    )
    identity_submitted_at = models.DateTimeField("实名认证提交时间", null=True, blank=True)
    identity_reviewed_at = models.DateTimeField("实名认证审核时间", null=True, blank=True)
    identity_rejection_reason = models.CharField("实名认证驳回原因", max_length=500, blank=True)
    onboarding_status = models.CharField(
        "开通审核状态",
        max_length=24,
        choices=OnboardingStatus,
        default=OnboardingStatus.INCOMPLETE,
    )
    onboarding_submitted_at = models.DateTimeField("开通审核提交时间", null=True, blank=True)
    onboarding_reviewed_at = models.DateTimeField("开通审核时间", null=True, blank=True)
    onboarding_reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_provider_onboardings",
        null=True,
        blank=True,
        verbose_name="开通审核人",
    )
    onboarding_rejection_reason = models.CharField("开通审核驳回原因", max_length=500, blank=True)
    is_accepting_orders = models.BooleanField("已开启接单", default=False)
    admin_order_restricted = models.BooleanField("后台限制接单", default=False)
    admin_restriction_reason = models.CharField("后台限制原因", max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_profile"
        indexes = [models.Index(fields=("status", "service_city_code"))]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(max_service_radius_km__gte=10)
                & models.Q(max_service_radius_km__lte=70),
                name="provider_radius_between_10_70",
            )
        ]
        verbose_name = "达人资料"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return str(self.user)

    @property
    def is_profile_complete(self) -> bool:
        return bool(
            self.display_name.strip()
            and self.bio.strip()
            and self.lifestyle_photo_id
            and self.service_city_code
            and self.service_city_name
        )

    @property
    def has_verified_identity(self) -> bool:
        return self.identity_status == self.IdentityStatus.VERIFIED

    @property
    def public_display_name(self) -> str:
        return self.display_name.strip() or self.user.nickname


class ProviderCategoryGrant(models.Model):
    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.CASCADE,
        related_name="category_grants",
        verbose_name="达人",
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.PROTECT,
        related_name="provider_grants",
        verbose_name="服务分类",
    )
    is_active = models.BooleanField("有效", default=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="granted_provider_categories",
        null=True,
        blank=True,
        verbose_name="授权人",
    )
    granted_at = models.DateTimeField("授权时间", auto_now_add=True)
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)

    class Meta:
        db_table = "provider_category_grant"
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "category"), name="uniq_provider_category_grant"
            )
        ]
        verbose_name = "达人服务分类授权"
        verbose_name_plural = verbose_name


class ProviderLiveLocation(models.Model):
    provider = models.OneToOneField(
        ProviderProfile,
        on_delete=models.CASCADE,
        related_name="live_location",
        verbose_name="达人",
    )
    session_id = models.UUIDField("接单会话ID", unique=True, null=True, blank=True)
    source_longitude = models.DecimalField("GCJ-02经度", max_digits=10, decimal_places=7)
    source_latitude = models.DecimalField("GCJ-02纬度", max_digits=10, decimal_places=7)
    position = models.PointField("实时位置（WGS84）", geography=True, srid=4326)
    accuracy_m = models.DecimalField(
        "定位精度（米）",
        max_digits=8,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    speed_mps = models.DecimalField(
        "速度（米/秒）",
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    located_at = models.DateTimeField("客户端定位时间")
    received_at = models.DateTimeField("服务端接收时间")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_live_location"
        indexes = [models.Index(fields=("received_at",))]
        verbose_name = "达人实时位置"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.provider} @ {self.received_at:%Y-%m-%d %H:%M:%S}"


class ProviderService(models.Model):
    class BillingType(models.TextChoices):
        HOURLY = "hourly", "按小时"
        PER_SESSION = "per_session", "按次"

    provider = models.ForeignKey(
        ProviderProfile, on_delete=models.CASCADE, related_name="services", verbose_name="达人"
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.PROTECT,
        related_name="provider_services",
        verbose_name="分类",
    )
    billing_type = models.CharField("计费方式", max_length=16, choices=BillingType)
    price_amount = models.PositiveBigIntegerField("价格（分）", validators=[MinValueValidator(1)])
    estimated_duration_minutes = models.PositiveIntegerField(
        "预计服务时长（分钟）", null=True, blank=True
    )
    description = models.TextField("服务说明", blank=True)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_service"
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "category", "billing_type"),
                name="uniq_provider_category_billing",
            )
        ]
        verbose_name = "达人服务"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.provider} - {self.category}"


class ProviderProfileRevision(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已驳回"

    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.CASCADE,
        related_name="profile_revisions",
        verbose_name="达人",
    )
    display_name = models.CharField("达人名称", max_length=30)
    bio = models.TextField("个人简介")
    lifestyle_photo = models.ForeignKey(
        "mediafiles.MediaAsset",
        on_delete=models.PROTECT,
        related_name="provider_profile_revisions",
        verbose_name="生活照",
    )
    service_city_code = models.CharField("服务城市编码", max_length=20)
    service_city_name = models.CharField("服务城市", max_length=50)
    max_service_radius_km = models.PositiveSmallIntegerField(
        "最大服务半径（公里）",
        default=10,
        validators=[MinValueValidator(10), MaxValueValidator(70)],
    )
    status = models.CharField("审核状态", max_length=16, choices=Status, default=Status.PENDING)
    submitted_at = models.DateTimeField("提交时间", auto_now_add=True)
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_provider_profile_revisions",
        null=True,
        blank=True,
    )
    rejection_reason = models.CharField("驳回原因", max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_profile_revision"
        ordering = ("-submitted_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("provider",),
                condition=models.Q(status="pending"),
                name="uniq_pending_provider_profile_revision",
            )
        ]
        verbose_name = "达人资料修订"
        verbose_name_plural = verbose_name


class ProviderProfileMedia(models.Model):
    provider = models.ForeignKey(ProviderProfile, on_delete=models.CASCADE, related_name="gallery_items")
    asset = models.ForeignKey("mediafiles.MediaAsset", on_delete=models.PROTECT)
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("provider", "position"), name="uniq_provider_media_position"),
            models.UniqueConstraint(fields=("provider", "asset"), name="uniq_provider_media_asset"),
        ]


class ProviderProfileRevisionMedia(models.Model):
    revision = models.ForeignKey(ProviderProfileRevision, on_delete=models.CASCADE, related_name="gallery_items")
    asset = models.ForeignKey("mediafiles.MediaAsset", on_delete=models.PROTECT)
    position = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("revision", "position"), name="uniq_revision_media_position"),
            models.UniqueConstraint(fields=("revision", "asset"), name="uniq_revision_media_asset"),
        ]


class ProviderServiceRevision(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已驳回"

    class Action(models.TextChoices):
        CREATE = "create", "新增"
        UPDATE = "update", "修改"
        REACTIVATE = "reactivate", "重新上架"

    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.CASCADE,
        related_name="service_revisions",
        verbose_name="达人",
    )
    service = models.ForeignKey(
        ProviderService,
        on_delete=models.SET_NULL,
        related_name="revisions",
        null=True,
        blank=True,
        verbose_name="原服务",
    )
    category = models.ForeignKey(
        ServiceCategory,
        on_delete=models.PROTECT,
        related_name="provider_service_revisions",
        verbose_name="分类",
    )
    action = models.CharField("变更类型", max_length=16, choices=Action)
    billing_type = models.CharField("计费方式", max_length=16, choices=ProviderService.BillingType)
    price_amount = models.PositiveBigIntegerField("价格（分）", validators=[MinValueValidator(1)])
    estimated_duration_minutes = models.PositiveIntegerField(
        "预计服务时长（分钟）", null=True, blank=True
    )
    description = models.TextField("服务说明", blank=True)
    status = models.CharField("审核状态", max_length=16, choices=Status, default=Status.PENDING)
    submitted_at = models.DateTimeField("提交时间", auto_now_add=True)
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_provider_service_revisions",
        null=True,
        blank=True,
    )
    rejection_reason = models.CharField("驳回原因", max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_service_revision"
        ordering = ("-submitted_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "category", "billing_type"),
                condition=models.Q(status="pending"),
                name="uniq_pending_provider_service_revision",
            )
        ]
        verbose_name = "达人服务修订"
        verbose_name_plural = verbose_name


class ProviderWeeklyAvailability(models.Model):
    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.CASCADE,
        related_name="weekly_availability",
        verbose_name="达人",
    )
    weekday = models.PositiveSmallIntegerField(
        "星期", choices=tuple((value, label) for value, label in enumerate("一二三四五六日"))
    )
    starts_at = models.TimeField("开始时间")
    ends_at = models.TimeField("结束时间")
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_weekly_availability"
        ordering = ("weekday", "starts_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "weekday", "starts_at", "ends_at"),
                name="uniq_provider_weekly_availability",
            )
        ]
        verbose_name = "达人每周可服务时段"
        verbose_name_plural = verbose_name

    def clean(self):
        if self.starts_at >= self.ends_at:
            raise ValidationError({"ends_at": "结束时间必须晚于开始时间。"})

    def __str__(self) -> str:
        return f"{self.provider} 周{self.get_weekday_display()} {self.starts_at}-{self.ends_at}"


class ProviderDateAvailability(models.Model):
    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.CASCADE,
        related_name="date_availability",
        verbose_name="达人",
    )
    date = models.DateField("日期")
    starts_at = models.TimeField("开始时间")
    ends_at = models.TimeField("结束时间")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "provider_date_availability"
        ordering = ("date", "starts_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "date", "starts_at", "ends_at"),
                name="uniq_provider_date_availability",
            )
        ]
        verbose_name = "达人临时可服务时段"
        verbose_name_plural = verbose_name

    def clean(self):
        if self.starts_at >= self.ends_at:
            raise ValidationError({"ends_at": "结束时间必须晚于开始时间。"})


class ProviderDateClosure(models.Model):
    provider = models.ForeignKey(
        ProviderProfile, on_delete=models.CASCADE, related_name="date_closures", verbose_name="达人"
    )
    date = models.DateField("休息日期")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "provider_date_closure"
        constraints = [
            models.UniqueConstraint(fields=("provider", "date"), name="uniq_provider_date_closure")
        ]
        verbose_name = "达人休息日"
        verbose_name_plural = verbose_name
