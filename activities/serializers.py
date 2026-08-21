from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from mediafiles.models import MediaAsset
from mediafiles.services import build_media_url

from .models import Activity, ActivityCategory, ActivityParticipation, ActivityPublishOrder


STANDARD_REFUND_SNAPSHOT = {
    "version": "standard-v1",
    "rules": [
        {"before_hours": 12, "principal_refund_percent": 100, "service_fee_refund_percent": 100},
        {"before_hours": 6, "principal_refund_percent": 100, "service_fee_refund_percent": 0},
        {"before_hours": 2, "principal_refund_percent": 70, "service_fee_refund_percent": 0},
        {"before_hours": 0, "principal_refund_percent": 0, "service_fee_refund_percent": 0},
    ],
}


class ActivityCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityCategory
        fields = ("name", "slug")


class ActivityPublishOrderSerializer(serializers.ModelSerializer):
    activity_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = ActivityPublishOrder
        fields = (
            "order_no", "activity_id", "aa_principal_amount",
            "platform_service_fee_amount", "payable_amount", "status", "paid_at",
        )


class ActivityCreateSerializer(serializers.Serializer):
    category_slug = serializers.SlugField()
    title = serializers.CharField(max_length=80)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    formation_deadline = serializers.DateTimeField()
    meeting_place_name = serializers.CharField(max_length=100)
    meeting_address = serializers.CharField(max_length=255)
    longitude = serializers.DecimalField(max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(max_digits=10, decimal_places=7)
    capacity = serializers.IntegerField(min_value=2, max_value=100)
    min_participants = serializers.IntegerField(min_value=2, max_value=100)
    description = serializers.CharField(max_length=2000)
    participation_rules = serializers.CharField(max_length=2000)
    aa_principal_amount = serializers.IntegerField(min_value=1, max_value=10_000_000)
    refund_template_version = serializers.ChoiceField(choices=("standard-v1",))
    cover_id = serializers.PrimaryKeyRelatedField(
        source="cover",
        queryset=MediaAsset.objects.filter(
            category=MediaAsset.Category.ACTIVITY_COVER,
            status=MediaAsset.Status.UPLOADED,
        ),
    )

    def validate_category_slug(self, value):
        try:
            return ActivityCategory.objects.get(slug=value, is_active=True)
        except ActivityCategory.DoesNotExist as exc:
            raise serializers.ValidationError("活动分类不存在或已停用。") from exc

    def validate(self, attrs):
        now = timezone.now()
        starts_at = attrs["starts_at"]
        if starts_at < now + timedelta(hours=48):
            raise serializers.ValidationError({"starts_at": "活动开始时间至少为发布后48小时。"})
        if starts_at > now + timedelta(days=30):
            raise serializers.ValidationError({"starts_at": "活动开始时间不得晚于发布后30天。"})
        if attrs["ends_at"] <= starts_at:
            raise serializers.ValidationError({"ends_at": "结束时间必须晚于开始时间。"})
        if not now < attrs["formation_deadline"] < starts_at:
            raise serializers.ValidationError({"formation_deadline": "成局截止时间须晚于当前时间且早于活动开始。"})
        if attrs["min_participants"] > attrs["capacity"]:
            raise serializers.ValidationError({"min_participants": "最少成局人数不能超过人数上限。"})
        request = self.context.get("request")
        if not request or attrs["cover"].owner_id != request.user.pk:
            raise serializers.ValidationError({"cover_id": "活动封面不存在或无权使用。"})
        return attrs


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


class MyActivityListQuerySerializer(serializers.Serializer):
    role = serializers.ChoiceField(
        required=False, default="joined", choices=("joined", "organized")
    )
    state = serializers.ChoiceField(
        required=False, default="all", choices=("all", "upcoming", "history")
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class ActivityListItemSerializer(serializers.ModelSerializer):
    category = serializers.CharField(source="category.name")
    category_slug = serializers.CharField(source="category.slug")
    organizer_public_id = serializers.UUIDField(source="organizer.public_id")
    organizer_nickname = serializers.CharField(source="organizer.nickname")
    organizer_avatar_url = serializers.SerializerMethodField()
    cover_url = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    participant_count = serializers.IntegerField(read_only=True)

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
            "participant_count",
        )

    def get_organizer_avatar_url(self, obj) -> str | None:
        return build_media_url(obj.organizer.avatar_object_key)

    def get_cover_url(self, obj) -> str | None:
        return build_media_url(obj.cover.object_key) if obj.cover_id else None

    def get_distance_km(self, obj) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.km, 1) if distance is not None else None


class MyActivityListItemSerializer(ActivityListItemSerializer):
    participation_status = serializers.CharField(read_only=True, allow_null=True)
    joined_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta(ActivityListItemSerializer.Meta):
        fields = ActivityListItemSerializer.Meta.fields + (
            "participation_status",
            "joined_at",
        )


class ActivityDetailSerializer(ActivityListItemSerializer):
    meeting_address = serializers.CharField()
    description = serializers.CharField()
    participation_rules = serializers.CharField()
    formation_deadline = serializers.DateTimeField()
    refund_template_version = serializers.CharField()
    refund_rule_snapshot = serializers.JSONField()
    is_joined = serializers.SerializerMethodField()
    is_organizer = serializers.SerializerMethodField()
    participation_status = serializers.SerializerMethodField()
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
            "is_joined",
            "is_organizer",
            "participation_status",
            "platform_service_fee_amount",
            "payable_amount",
            "organizer_verified",
            "organizer_rating",
        )

    def get_platform_service_fee_amount(self, obj) -> int:
        return round(obj.aa_principal_amount * 0.1)

    def get_payable_amount(self, obj) -> int:
        return obj.aa_principal_amount + self.get_platform_service_fee_amount(obj)

    def get_organizer_verified(self, obj) -> bool:
        return obj.organizer.verification_status == obj.organizer.VerificationStatus.VERIFIED

    def get_organizer_rating(self, obj) -> str | None:
        profile = getattr(obj.organizer, "provider_profile", None)
        return str(profile.rating) if profile else None

    def _participation(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        cache = self.context.setdefault("activity_participation_cache", {})
        if obj.pk not in cache:
            cache[obj.pk] = ActivityParticipation.objects.filter(
                activity=obj,
                user=request.user,
            ).first()
        return cache[obj.pk]

    def get_is_joined(self, obj) -> bool:
        participation = self._participation(obj)
        return bool(participation and participation.status == ActivityParticipation.Status.ACTIVE)

    def get_is_organizer(self, obj) -> bool:
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.organizer_id == request.user.pk)

    def get_participation_status(self, obj) -> str | None:
        participation = self._participation(obj)
        return participation.status if participation else None


class ActivityParticipationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityParticipation
        fields = ("status", "joined_at")
