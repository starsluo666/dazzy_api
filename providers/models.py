from decimal import Decimal

from django.conf import settings
from django.contrib.gis.db import models
from django.core.validators import MinValueValidator


class ServiceCategory(models.Model):
    name = models.CharField("名称", max_length=30)
    slug = models.SlugField("标识", max_length=40, unique=True)
    icon_object_key = models.CharField("图标对象键", max_length=512, blank=True)
    city_codes = models.JSONField("展示城市编码", default=list, blank=True)
    sort_order = models.PositiveIntegerField("排序", default=0)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_service_category"
        ordering = ("sort_order", "id")
        verbose_name = "达人服务分类"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.name


class ProviderProfile(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已驳回"
        SUSPENDED = "suspended", "已暂停"

    class MapSource(models.TextChoices):
        AMAP = "amap", "高德地图"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="provider_profile",
        verbose_name="用户",
    )
    status = models.CharField("审核状态", max_length=16, choices=Status, default=Status.DRAFT)
    bio = models.TextField("个人简介", blank=True)
    service_city_code = models.CharField("服务城市编码", max_length=20, blank=True)
    service_city_name = models.CharField("服务城市", max_length=50, blank=True)
    map_source = models.CharField(
        "地图来源", max_length=16, choices=MapSource, default=MapSource.AMAP
    )
    source_longitude = models.DecimalField(
        "原始GCJ-02经度", max_digits=10, decimal_places=7, null=True, blank=True
    )
    source_latitude = models.DecimalField(
        "原始GCJ-02纬度", max_digits=10, decimal_places=7, null=True, blank=True
    )
    service_center = models.PointField(
        "服务中心点（WGS84）", geography=True, srid=4326, null=True, blank=True
    )
    max_service_radius_km = models.PositiveSmallIntegerField("最大服务半径（公里）", default=10)
    rating = models.DecimalField(
        "评分",
        max_digits=3,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    service_count = models.PositiveIntegerField("服务次数", default=0)
    order_count = models.PositiveIntegerField("接单量", default=0)
    credit_score = models.PositiveSmallIntegerField("信用分", default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "provider_profile"
        indexes = [models.Index(fields=("status", "service_city_code"))]
        verbose_name = "达人资料"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return str(self.user)


class ProviderService(models.Model):
    class BillingType(models.TextChoices):
        HOURLY = "hourly", "按小时"
        PER_SESSION = "per_session", "按次"

    provider = models.ForeignKey(
        ProviderProfile, on_delete=models.CASCADE, related_name="services", verbose_name="达人"
    )
    category = models.ForeignKey(
        ServiceCategory, on_delete=models.PROTECT, related_name="provider_services", verbose_name="分类"
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
