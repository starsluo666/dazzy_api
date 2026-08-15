from rest_framework import serializers

from mediafiles.services import build_media_url

from .models import ProviderProfile, ProviderService


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
    avatar_url = serializers.SerializerMethodField()
    verified = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    services = ProviderServiceSummarySerializer(many=True)

    class Meta:
        model = ProviderProfile
        fields = (
            "public_id",
            "nickname",
            "avatar_url",
            "verified",
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

    def get_distance_km(self, obj) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.km, 1) if distance is not None else None
