from decimal import Decimal

from rest_framework import serializers
from django.utils import timezone

from mediafiles.models import MediaAsset
from mediafiles.services import build_media_url
from orders.models import ProviderOrderReview

from .models import ProviderProfile, ProviderService, ServiceCategory
from .presence import MAX_LOCATION_ACCURACY_M, provider_is_online


class ProviderListQuerySerializer(serializers.Serializer):
    category = serializers.SlugField(required=False)
    city_code = serializers.CharField(required=False, max_length=20)
    longitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    ordering = serializers.ChoiceField(
        required=False,
        default="recommended",
        choices=("recommended", "distance", "rating", "price"),
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)

    def validate(self, attrs):
        if ("longitude" in attrs) != ("latitude" in attrs):
            raise serializers.ValidationError("longitude 和 latitude 必须同时提供。")
        if attrs.get("ordering") == "distance" and "longitude" not in attrs:
            raise serializers.ValidationError("按距离排序时必须提供经纬度。")
        return attrs


class ProviderAvailabilityQuerySerializer(serializers.Serializer):
    service_id = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False)
    days = serializers.IntegerField(required=False, default=4, min_value=1, max_value=7)
    duration_minutes = serializers.IntegerField(required=False, min_value=30, max_value=480)


class ProviderReviewQuerySerializer(serializers.Serializer):
    rating = serializers.IntegerField(required=False, min_value=1, max_value=5)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=10, min_value=1, max_value=30)


class PublicProviderReviewSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    service_name = serializers.CharField(source="order.service_name_snapshot")
    image_urls = serializers.SerializerMethodField()

    class Meta:
        model = ProviderOrderReview
        fields = (
            "id", "customer_name", "rating", "content", "service_name", "image_urls",
            "created_at",
        )

    def get_customer_name(self, obj):
        return "匿名用户" if obj.is_anonymous else obj.customer.nickname

    def get_image_urls(self, obj):
        return [build_media_url(image.object_key) for image in obj.images.all()]


class ServiceCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceCategory
        fields = ("id", "name", "slug")


class ProviderApplicationSerializer(serializers.ModelSerializer):
    verification_status = serializers.CharField(source="user.verification_status", read_only=True)
    gender = serializers.CharField(source="user.gender", read_only=True)
    lifestyle_photo_id = serializers.PrimaryKeyRelatedField(
        source="lifestyle_photo",
        queryset=MediaAsset.objects.filter(
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
        ),
        allow_null=True,
        required=False,
    )
    lifestyle_photo_url = serializers.SerializerMethodField()

    class Meta:
        model = ProviderProfile
        fields = (
            "status",
            "verification_status",
            "gender",
            "bio",
            "lifestyle_photo_id",
            "lifestyle_photo_url",
            "service_city_code",
            "service_city_name",
            "max_service_radius_km",
            "invitation_code",
            "agreement_accepted_at",
            "submitted_at",
            "reviewed_at",
            "rejection_reason",
            "updated_at",
        )
        read_only_fields = (
            "status",
            "verification_status",
            "gender",
            "lifestyle_photo_url",
            "agreement_accepted_at",
            "submitted_at",
            "reviewed_at",
            "rejection_reason",
            "updated_at",
        )

    def validate_bio(self, value: str) -> str:
        value = value.strip()
        if value and len(value) < 10:
            raise serializers.ValidationError("达人简介至少填写10个字。")
        return value

    def validate_lifestyle_photo_id(self, value):
        request = self.context.get("request")
        if value and (request is None or value.owner_id != request.user.pk):
            raise serializers.ValidationError("生活照不存在或无权使用。")
        return value

    def get_lifestyle_photo_url(self, obj) -> str | None:
        if not obj.lifestyle_photo_id:
            return None
        return build_media_url(obj.lifestyle_photo.object_key)


class ProviderApplicationSubmitSerializer(serializers.Serializer):
    agreement_accepted = serializers.BooleanField()

    def validate_agreement_accepted(self, value):
        if not value:
            raise serializers.ValidationError("请阅读并同意达人服务声明及平台协议。")
        return value


class ProviderServiceManageSerializer(serializers.ModelSerializer):
    category_id = serializers.PrimaryKeyRelatedField(
        source="category", queryset=ServiceCategory.objects.filter(is_active=True)
    )
    category = serializers.CharField(source="category.name", read_only=True)
    category_slug = serializers.CharField(source="category.slug", read_only=True)

    class Meta:
        model = ProviderService
        fields = (
            "id",
            "category_id",
            "category",
            "category_slug",
            "billing_type",
            "price_amount",
            "estimated_duration_minutes",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        billing_type = attrs.get("billing_type", getattr(self.instance, "billing_type", None))
        category = attrs.get("category", getattr(self.instance, "category", None))
        duration = attrs.get(
            "estimated_duration_minutes", getattr(self.instance, "estimated_duration_minutes", None)
        )
        if attrs.get("is_active") is True and category and not category.is_active:
            raise serializers.ValidationError(
                {"is_active": "该服务分类已停用，暂时不能上架服务。"}
            )
        if billing_type == ProviderService.BillingType.PER_SESSION and not duration:
            raise serializers.ValidationError(
                {"estimated_duration_minutes": "按次服务必须填写预计服务时长。"}
            )
        return attrs


class ProviderScheduleQuerySerializer(serializers.Serializer):
    start_date = serializers.DateField(required=False)
    days = serializers.IntegerField(required=False, default=7, min_value=1, max_value=14)


class ProviderScheduleCreateSerializer(serializers.Serializer):
    date = serializers.DateField()
    starts_at = serializers.TimeField()
    ends_at = serializers.TimeField()
    repeat_weekly = serializers.BooleanField(default=False)
    copy_weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6), required=False, default=list
    )

    def validate(self, attrs):
        if attrs["starts_at"] >= attrs["ends_at"]:
            raise serializers.ValidationError({"ends_at": "结束时间必须晚于开始时间。"})
        if attrs["date"] < timezone.localdate():
            raise serializers.ValidationError({"date": "不能为过去的日期设置可预约时段。"})
        if attrs["copy_weekdays"] and not attrs["repeat_weekly"]:
            raise serializers.ValidationError(
                {"copy_weekdays": "复制到其他星期时必须开启每周重复。"}
            )
        return attrs


class ProviderDateClosureSerializer(serializers.Serializer):
    is_closed = serializers.BooleanField()


class ProviderLiveLocationInputSerializer(serializers.Serializer):
    longitude = serializers.DecimalField(
        max_digits=10,
        decimal_places=7,
        min_value=Decimal("-180"),
        max_value=Decimal("180"),
    )
    latitude = serializers.DecimalField(
        max_digits=10,
        decimal_places=7,
        min_value=Decimal("-90"),
        max_value=Decimal("90"),
    )
    accuracy_m = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0"),
        max_value=MAX_LOCATION_ACCURACY_M,
    )
    speed_mps = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0"),
        max_value=Decimal("100"),
    )
    located_at = serializers.DateTimeField(required=False)


class ProviderLiveLocationUpdateSerializer(ProviderLiveLocationInputSerializer):
    session_id = serializers.UUIDField()


class ProviderServiceSummarySerializer(serializers.ModelSerializer):
    category = serializers.CharField(source="category.name")
    category_slug = serializers.CharField(source="category.slug")

    class Meta:
        model = ProviderService
        fields = (
            "id",
            "category",
            "category_slug",
            "billing_type",
            "price_amount",
            "estimated_duration_minutes",
        )


class ProviderListItemSerializer(serializers.ModelSerializer):
    public_id = serializers.UUIDField(source="user.public_id")
    nickname = serializers.CharField(source="user.nickname")
    birth_date = serializers.DateField(source="user.birth_date", allow_null=True)
    avatar_url = serializers.SerializerMethodField()
    verified = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    services = ProviderServiceSummarySerializer(many=True)

    class Meta:
        model = ProviderProfile
        fields = (
            "public_id",
            "nickname",
            "birth_date",
            "avatar_url",
            "verified",
            "is_online",
            "service_city_name",
            "bio",
            "rating",
            "service_count",
            "order_count",
            "distance_km",
            "services",
        )

    def get_avatar_url(self, obj) -> str | None:
        return build_media_url(obj.user.avatar_object_key)

    def get_verified(self, obj) -> bool:
        return obj.user.verification_status == obj.user.VerificationStatus.VERIFIED

    def get_is_online(self, obj) -> bool:
        annotated = getattr(obj, "is_currently_online", None)
        return annotated if annotated is not None else provider_is_online(obj)

    def get_distance_km(self, obj) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.km, 1) if distance is not None else None


class ProviderDetailSerializer(ProviderListItemSerializer):
    gender = serializers.CharField(source="user.gender")
    birth_date = serializers.DateField(source="user.birth_date", allow_null=True)
    lifestyle_photo_url = serializers.SerializerMethodField()
    credit_score = serializers.IntegerField()
    max_service_radius_km = serializers.IntegerField()
    is_favorited = serializers.SerializerMethodField()

    class Meta(ProviderListItemSerializer.Meta):
        fields = ProviderListItemSerializer.Meta.fields + (
            "gender",
            "birth_date",
            "lifestyle_photo_url",
            "credit_score",
            "max_service_radius_km",
            "is_favorited",
        )

    def get_is_favorited(self, obj) -> bool:
        request = self.context.get("request")
        return bool(
            request
            and request.user.is_authenticated
            and obj.favorited_by.filter(user=request.user).exists()
        )

    def get_lifestyle_photo_url(self, obj) -> str | None:
        if not obj.lifestyle_photo_id:
            return None
        return build_media_url(obj.lifestyle_photo.object_key)
