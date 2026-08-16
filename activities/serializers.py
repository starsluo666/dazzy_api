from rest_framework import serializers

from mediafiles.services import build_media_url

from .models import Activity


class ActivityListQuerySerializer(serializers.Serializer):
    category = serializers.SlugField(required=False)
    longitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    ordering = serializers.ChoiceField(
        required=False,
        default="recommended",
        choices=("recommended", "distance", "time", "latest"),
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)

    def validate(self, attrs):
        if ("longitude" in attrs) != ("latitude" in attrs):
            raise serializers.ValidationError("longitude 和 latitude 必须同时提供。")
        if attrs.get("ordering") == "distance" and "longitude" not in attrs:
            raise serializers.ValidationError("按距离排序时必须提供经纬度。")
        return attrs


class ActivityListItemSerializer(serializers.ModelSerializer):
    category = serializers.CharField(source="category.name")
    category_slug = serializers.CharField(source="category.slug")
    organizer_public_id = serializers.UUIDField(source="organizer.public_id")
    organizer_nickname = serializers.CharField(source="organizer.nickname")
    organizer_avatar_url = serializers.SerializerMethodField()
    cover_url = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = Activity
        fields = (
            "id",
            "title",
            "category",
            "category_slug",
            "organizer_public_id",
            "organizer_nickname",
            "organizer_avatar_url",
            "cover_url",
            "starts_at",
            "ends_at",
            "meeting_place_name",
            "capacity",
            "min_participants",
            "aa_principal_amount",
            "status",
            "distance_km",
        )

    def get_organizer_avatar_url(self, obj) -> str | None:
        return build_media_url(obj.organizer.avatar_object_key)

    def get_cover_url(self, obj) -> str | None:
        return build_media_url(obj.cover.object_key) if obj.cover_id else None

    def get_distance_km(self, obj) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.km, 1) if distance is not None else None


class ActivityDetailSerializer(ActivityListItemSerializer):
    meeting_address = serializers.CharField()
    description = serializers.CharField()
    participation_rules = serializers.CharField()
    formation_deadline = serializers.DateTimeField()
    refund_template_version = serializers.CharField()
    refund_rule_snapshot = serializers.JSONField()
    participant_count = serializers.SerializerMethodField()
    platform_service_fee_amount = serializers.SerializerMethodField()
    payable_amount = serializers.SerializerMethodField()
    organizer_verified = serializers.SerializerMethodField()
    organizer_rating = serializers.SerializerMethodField()

    class Meta(ActivityListItemSerializer.Meta):
        fields = ActivityListItemSerializer.Meta.fields + (
            "meeting_address",
            "description",
            "participation_rules",
            "formation_deadline",
            "refund_template_version",
            "refund_rule_snapshot",
            "participant_count",
            "platform_service_fee_amount",
            "payable_amount",
            "organizer_verified",
            "organizer_rating",
        )

    def get_participant_count(self, obj) -> int:
        return 0

    def get_platform_service_fee_amount(self, obj) -> int:
        return round(obj.aa_principal_amount * 0.1)

    def get_payable_amount(self, obj) -> int:
        return obj.aa_principal_amount + self.get_platform_service_fee_amount(obj)

    def get_organizer_verified(self, obj) -> bool:
        return obj.organizer.verification_status == obj.organizer.VerificationStatus.VERIFIED

    def get_organizer_rating(self, obj) -> str | None:
        profile = getattr(obj.organizer, "provider_profile", None)
        return str(profile.rating) if profile else None
