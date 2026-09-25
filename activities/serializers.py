from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from backoffice.operation_settings import platform_operation_rules
from mediafiles.models import MediaAsset
from mediafiles.services import build_media_url

from .models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivityReport,
    ActivitySettlement,
)
from .pricing import calculate_publish_service_fee


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
    icon_url = serializers.SerializerMethodField()

    class Meta:
        model = ActivityCategory
        fields = (
            "name", "slug", "icon_url", "min_capacity", "max_capacity",
            "min_aa_principal_amount", "max_aa_principal_amount", "content_guidance",
        )

    def get_icon_url(self, obj):
        return build_media_url(obj.icon_object_key) if obj.icon_object_key else None


class ActivityPublishOrderSerializer(serializers.ModelSerializer):
    activity_id = serializers.IntegerField(read_only=True)
    wallet_amount = serializers.SerializerMethodField()
    external_amount = serializers.SerializerMethodField()

    def _breakdown(self, obj):
        from wallets.models import WalletPaymentAllocation
        from wallets.services import preview_wallet_payment

        cache = getattr(self, "_breakdown_cache", {})
        if obj.order_no not in cache:
            cache[obj.order_no] = preview_wallet_payment(
                user_id=obj.payer_id,
                payable_amount=obj.payable_amount,
                business_type=WalletPaymentAllocation.BusinessType.ACTIVITY_PUBLISH,
                business_order_no=obj.order_no,
                external_only=obj.status != obj.Status.PENDING_PAYMENT or hasattr(obj, "huifu_payment"),
            )
            self._breakdown_cache = cache
        return cache[obj.order_no]

    def get_wallet_amount(self, obj):
        return self._breakdown(obj).wallet_amount

    def get_external_amount(self, obj):
        return self._breakdown(obj).external_amount

    class Meta:
        model = ActivityPublishOrder
        fields = (
            "order_no", "activity_id", "aa_principal_amount",
            "platform_service_fee_amount", "payable_amount", "status", "expires_at",
            "paid_at", "closed_at", "wallet_amount", "external_amount",
        )


class ActivityParticipationPaymentOrderSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    channel_label = serializers.CharField(source="get_channel_display")
    wallet_amount = serializers.SerializerMethodField()
    external_amount = serializers.SerializerMethodField()

    def _breakdown(self, obj):
        from wallets.models import WalletPaymentAllocation
        from wallets.services import preview_wallet_payment

        cache = getattr(self, "_breakdown_cache", {})
        if obj.order_no not in cache:
            cache[obj.order_no] = preview_wallet_payment(
                user_id=obj.payer_id,
                payable_amount=obj.payable_amount,
                business_type=WalletPaymentAllocation.BusinessType.ACTIVITY_PARTICIPATION,
                business_order_no=obj.order_no,
                external_only=obj.status != obj.Status.PENDING_PAYMENT or hasattr(obj, "huifu_payment"),
            )
            self._breakdown_cache = cache
        return cache[obj.order_no]

    def get_wallet_amount(self, obj):
        return self._breakdown(obj).wallet_amount

    def get_external_amount(self, obj):
        return self._breakdown(obj).external_amount

    class Meta:
        model = ActivityParticipationPaymentOrder
        fields = (
            "order_no", "aa_principal_amount", "platform_service_fee_amount",
            "payable_amount", "channel", "channel_label", "status", "status_label",
            "expires_at", "paid_at", "closed_at", "wallet_amount", "external_amount",
        )


class ActivityParticipationRefundOrderSerializer(serializers.ModelSerializer):
    refund_type_label = serializers.CharField(source="get_refund_type_display")
    status_label = serializers.CharField(source="get_status_display")
    retained_principal_destination_label = serializers.CharField(
        source="get_retained_principal_destination_display"
    )

    class Meta:
        model = ActivityParticipationRefundOrder
        fields = (
            "refund_no", "refund_type", "refund_type_label", "status", "status_label",
            "principal_refund_amount", "service_fee_refund_amount", "refund_amount",
            "wallet_refund_amount", "external_refund_amount",
            "retained_principal_amount", "retained_service_fee_amount",
            "retained_principal_destination", "retained_principal_destination_label",
            "reason", "failure_reason", "requested_at", "refunded_at",
        )


class ActivitySettlementSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    settlement_amount = serializers.SerializerMethodField()
    organizer_principal_amount = serializers.SerializerMethodField()
    participant_principal_amount = serializers.SerializerMethodField()
    retained_participant_principal_amount = serializers.SerializerMethodField()

    class Meta:
        model = ActivitySettlement
        fields = (
            "settlement_no", "status", "status_label", "confirmation_started_at",
            "confirmation_deadline", "risk_frozen_at", "freeze_until", "settled_at",
            "dispute_reason", "settlement_amount", "organizer_principal_amount",
            "participant_principal_amount", "retained_participant_principal_amount",
        )

    def _organizer_amount(self, obj, field):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        return getattr(obj, field) if request.user.pk == obj.beneficiary_id else None

    def get_settlement_amount(self, obj):
        return self._organizer_amount(obj, "settlement_amount")

    def get_organizer_principal_amount(self, obj):
        return self._organizer_amount(obj, "organizer_principal_amount")

    def get_participant_principal_amount(self, obj):
        return self._organizer_amount(obj, "participant_principal_amount")

    def get_retained_participant_principal_amount(self, obj):
        return self._organizer_amount(obj, "retained_participant_principal_amount")


class ActivityAfterSalesCaseSerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source="get_reason_display")
    status_label = serializers.CharField(source="get_status_display")
    refund_order = ActivityParticipationRefundOrderSerializer(read_only=True)

    class Meta:
        model = ActivityAfterSalesCase
        fields = (
            "case_no", "reason", "reason_label", "description", "status",
            "status_label", "requested_principal_amount",
            "requested_service_fee_amount", "requested_amount",
            "approved_principal_amount", "approved_service_fee_amount",
            "approved_amount", "result_note", "reviewed_at", "refund_order",
            "created_at", "updated_at",
        )


class ActivityParticipationCheckoutSerializer(serializers.Serializer):
    participation_status = serializers.CharField(source="participation.status")
    rule_confirmed_at = serializers.DateTimeField(source="participation.rule_confirmed_at")
    payment_order = ActivityParticipationPaymentOrderSerializer(source="order")
    participant_count = serializers.IntegerField()
    remaining_capacity = serializers.IntegerField()


class ActivityParticipationOrderCreateSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(
        required=False,
        default=ActivityParticipationPaymentOrder.Channel.MOCK_WECHAT,
        choices=(
            ActivityParticipationPaymentOrder.Channel.MOCK_WECHAT,
            ActivityParticipationPaymentOrder.Channel.MOCK_ALIPAY,
            ActivityParticipationPaymentOrder.Channel.WECHAT,
        ),
    )


class ActivityParticipationCancellationSerializer(serializers.Serializer):
    reason = serializers.CharField(
        required=False, default="用户主动取消报名", max_length=500, trim_whitespace=True
    )


class ActivityOrganizerCancellationSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=2, max_length=500, trim_whitespace=True)


class ActivityAfterSalesCreateSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=ActivityAfterSalesCase.Reason.choices)
    description = serializers.CharField(min_length=5, max_length=1000)
    evidence_asset_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        required=False,
        allow_empty=True,
        queryset=MediaAsset.objects.filter(
            category=MediaAsset.Category.SUPPORT_ATTACHMENT,
            status=MediaAsset.Status.UPLOADED,
        ),
    )

    def validate_evidence_asset_ids(self, value):
        request = self.context.get("request")
        if len(value) > 3:
            raise serializers.ValidationError("证明材料最多上传3张。")
        if not request or any(asset.owner_id != request.user.pk for asset in value):
            raise serializers.ValidationError("证明材料不存在或无权使用。")
        return value


class ActivityCreateSerializer(serializers.Serializer):
    tag_slugs = serializers.ListField(
        child=serializers.SlugField(),
        min_length=1,
        max_length=5,
        required=False,
    )
    # Kept during the client migration window; new clients submit tag_slugs.
    category_slug = serializers.SlugField(required=False)
    title = serializers.CharField(max_length=80)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    formation_deadline = serializers.DateTimeField()
    meeting_place_name = serializers.CharField(max_length=100)
    meeting_address = serializers.CharField(max_length=255)
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    city_name = serializers.CharField(required=False, allow_blank=True, max_length=50)
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
        required=False,
        allow_null=True,
        queryset=MediaAsset.objects.filter(
            category=MediaAsset.Category.ACTIVITY_COVER,
            status=MediaAsset.Status.UPLOADED,
        ),
    )

    def validate_tag_slugs(self, value):
        normalized = list(dict.fromkeys(value))
        tags = list(
            ActivityCategory.objects.filter(slug__in=normalized, is_active=True)
        )
        tags_by_slug = {tag.slug: tag for tag in tags}
        missing = [slug for slug in normalized if slug not in tags_by_slug]
        if missing:
            raise serializers.ValidationError(f"活动标签不存在或已停用：{', '.join(missing)}。")
        return [tags_by_slug[slug] for slug in normalized]

    def validate_category_slug(self, value):
        try:
            return ActivityCategory.objects.get(slug=value, is_active=True)
        except ActivityCategory.DoesNotExist as exc:
            raise serializers.ValidationError("活动标签不存在或已停用。") from exc

    def validate(self, attrs):
        now = timezone.now()
        starts_at = attrs["starts_at"]
        rules = platform_operation_rules()
        minimum_hours = rules["activity_minimum_advance_hours"]
        maximum_days = rules["activity_maximum_advance_days"]
        if starts_at < now + timedelta(hours=minimum_hours):
            raise serializers.ValidationError(
                {"starts_at": f"活动开始时间至少为发布后{minimum_hours}小时。"}
            )
        if starts_at > now + timedelta(days=maximum_days):
            raise serializers.ValidationError(
                {"starts_at": f"活动开始时间不得晚于发布后{maximum_days}天。"}
            )
        if attrs["ends_at"] <= starts_at:
            raise serializers.ValidationError({"ends_at": "结束时间必须晚于开始时间。"})
        if not now < attrs["formation_deadline"] < starts_at:
            raise serializers.ValidationError({"formation_deadline": "成局截止时间须晚于当前时间且早于活动开始。"})
        if attrs["min_participants"] > attrs["capacity"]:
            raise serializers.ValidationError({"min_participants": "最少成局人数不能超过人数上限。"})
        tags = attrs.get("tag_slugs", [])
        category = attrs.get("category_slug")
        if not tags and not category:
            raise serializers.ValidationError({"tag_slugs": "请至少选择一个活动标签。"})
        city_code = attrs.get("city_code", "")
        unavailable_tags = [
            tag.name for tag in tags if tag.city_codes and city_code not in tag.city_codes
        ]
        if unavailable_tags:
            raise serializers.ValidationError(
                {"tag_slugs": f"以下活动标签未在当前城市开放：{'、'.join(unavailable_tags)}。"}
            )
        if category and not tags and category.city_codes and city_code not in category.city_codes:
            raise serializers.ValidationError({"category_slug": "该活动标签未在当前城市开放。"})
        min_capacity = rules["activity_min_capacity"]
        max_capacity = rules["activity_max_capacity"]
        min_amount = rules["activity_min_aa_principal_amount"]
        max_amount = rules["activity_max_aa_principal_amount"]
        if not min_capacity <= attrs["capacity"] <= max_capacity:
            raise serializers.ValidationError(
                {"capacity": f"活动人数范围为 {min_capacity}—{max_capacity} 人。"}
            )
        if not min_amount <= attrs["aa_principal_amount"] <= max_amount:
            raise serializers.ValidationError(
                {"aa_principal_amount": "AA本金超出平台允许范围。"}
            )
        request = self.context.get("request")
        cover = attrs.get("cover")
        from backoffice.models import PlatformOperationSetting

        operation_setting = PlatformOperationSetting.current()
        if cover and (
            not request
            or (
                cover.owner_id != request.user.pk
                and cover.pk != operation_setting.default_activity_cover_id
                and not Activity.objects.filter(
                    organizer=request.user, cover=cover
                ).exists()
            )
        ):
            raise serializers.ValidationError({"cover_id": "活动封面不存在或无权使用。"})
        if not cover:
            if not operation_setting.default_activity_cover_id:
                raise serializers.ValidationError(
                    {"cover_id": "平台尚未配置默认活动封面，请上传活动封面。"}
                )
        return attrs


class ActivityListQuerySerializer(serializers.Serializer):
    category = serializers.SlugField(required=False)
    tags = serializers.CharField(required=False, allow_blank=True, max_length=220)
    keyword = serializers.CharField(required=False, allow_blank=True, max_length=80)
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    longitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    ordering = serializers.ChoiceField(
        required=False,
        default="recommended",
        choices=("recommended", "distance", "time", "latest", "popular"),
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)

    def validate(self, attrs):
        if ("longitude" in attrs) != ("latitude" in attrs):
            raise serializers.ValidationError("longitude 和 latitude 必须同时提供。")
        if attrs.get("ordering") == "distance" and "longitude" not in attrs:
            raise serializers.ValidationError("按距离排序时必须提供经纬度。")
        raw_tags = attrs.get("tags", "")
        attrs["tags"] = list(
            dict.fromkeys(item.strip() for item in raw_tags.split(",") if item.strip())
        )[:5]
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
    category = serializers.SerializerMethodField()
    category_slug = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
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
            "tags",
            "organizer_public_id",
            "organizer_nickname",
            "organizer_avatar_url",
            "cover_url",
            "starts_at",
            "ends_at",
            "meeting_place_name",
            "city_code",
            "city_name",
            "capacity",
            "min_participants",
            "aa_principal_amount",
            "status",
            "distance_km",
            "participant_count",
        )

    def get_organizer_avatar_url(self, obj) -> str | None:
        return build_media_url(obj.organizer.avatar_object_key)

    def _tags(self, obj):
        tags = list(obj.tags.all())
        if not tags and obj.category_id:
            tags = [obj.category]
        return tags

    def get_category(self, obj) -> str:
        tags = self._tags(obj)
        return tags[0].name if tags else "活动"

    def get_category_slug(self, obj) -> str:
        tags = self._tags(obj)
        return tags[0].slug if tags else ""

    def get_tags(self, obj):
        return [
            {
                "name": tag.name,
                "slug": tag.slug,
                "icon_url": build_media_url(tag.icon_object_key)
                if tag.icon_object_key else None,
            }
            for tag in self._tags(obj)
        ]

    def get_cover_url(self, obj) -> str | None:
        return build_media_url(obj.cover.object_key) if obj.cover_id else None

    def get_distance_km(self, obj) -> float | None:
        distance = getattr(obj, "distance", None)
        return round(distance.km, 1) if distance is not None else None


class MyActivityListItemSerializer(ActivityListItemSerializer):
    participation_status = serializers.CharField(read_only=True, allow_null=True)
    joined_at = serializers.DateTimeField(read_only=True, allow_null=True)
    participation_payment_expires_at = serializers.DateTimeField(
        read_only=True, allow_null=True
    )
    participation_refund_status = serializers.CharField(read_only=True, allow_null=True)
    participation_after_sales_status = serializers.CharField(
        read_only=True, allow_null=True
    )
    settlement = serializers.SerializerMethodField()

    class Meta(ActivityListItemSerializer.Meta):
        fields = ActivityListItemSerializer.Meta.fields + (
            "participation_status",
            "joined_at",
            "participation_payment_expires_at",
            "participation_refund_status",
            "participation_after_sales_status",
            "settlement",
            "reviewed_at",
            "rejection_reason",
            "cancelled_at",
            "cancellation_reason",
        )

    def get_settlement(self, obj):
        settlement = getattr(obj, "settlement", None)
        if not settlement:
            return None
        return ActivitySettlementSerializer(
            settlement, context=self.context
        ).data


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
    participation_payment_expires_at = serializers.SerializerMethodField()
    participation_refund = serializers.SerializerMethodField()
    participation_after_sales = serializers.SerializerMethodField()
    settlement = serializers.SerializerMethodField()
    locked_seat_count = serializers.SerializerMethodField()
    remaining_capacity = serializers.SerializerMethodField()
    platform_service_fee_amount = serializers.SerializerMethodField()
    payable_amount = serializers.SerializerMethodField()
    service_fee_rate = serializers.DecimalField(max_digits=5, decimal_places=4)
    organizer_rating = serializers.SerializerMethodField()
    reviewed_at = serializers.DateTimeField(allow_null=True)
    rejection_reason = serializers.CharField()

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
            "participation_payment_expires_at",
            "participation_refund",
            "participation_after_sales",
            "settlement",
            "locked_seat_count",
            "remaining_capacity",
            "platform_service_fee_amount",
            "payable_amount",
            "service_fee_rate",
            "organizer_rating",
            "reviewed_at",
            "rejection_reason",
        )

    def get_platform_service_fee_amount(self, obj) -> int:
        return calculate_publish_service_fee(obj.aa_principal_amount, obj.service_fee_rate)

    def get_payable_amount(self, obj) -> int:
        return obj.aa_principal_amount + self.get_platform_service_fee_amount(obj)

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

    def get_participation_payment_expires_at(self, obj):
        participation = self._participation(obj)
        return participation.payment_expires_at if participation else None

    def get_participation_refund(self, obj):
        participation = self._participation(obj)
        if not participation:
            return None
        refund = participation.refund_orders.order_by("-created_at").first()
        return ActivityParticipationRefundOrderSerializer(refund).data if refund else None

    def get_participation_after_sales(self, obj):
        participation = self._participation(obj)
        if not participation:
            return None
        case = participation.after_sales_cases.order_by("-created_at").first()
        return ActivityAfterSalesCaseSerializer(case).data if case else None

    def get_settlement(self, obj):
        settlement = getattr(obj, "settlement", None)
        if not settlement:
            return None
        return ActivitySettlementSerializer(
            settlement, context=self.context
        ).data

    def get_locked_seat_count(self, obj) -> int:
        return ActivityParticipation.objects.filter(
            activity=obj,
            status=ActivityParticipation.Status.PENDING_PAYMENT,
            payment_expires_at__gt=timezone.now(),
        ).count()

    def get_remaining_capacity(self, obj) -> int:
        return max(
            0,
            obj.capacity - obj.participant_count - self.get_locked_seat_count(obj),
        )


class ActivityParticipationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityParticipation
        fields = (
            "status", "aa_principal_amount", "platform_service_fee_amount",
            "payable_amount", "rule_confirmed_at", "payment_expires_at", "joined_at",
            "cancelled_at", "cancellation_reason", "cancelled_by_role",
        )


class ActivityCopySourceSerializer(serializers.ModelSerializer):
    category_slug = serializers.SerializerMethodField()
    tag_slugs = serializers.SerializerMethodField()
    cover_id = serializers.UUIDField(allow_null=True)
    cover_url = serializers.SerializerMethodField()
    longitude = serializers.DecimalField(
        source="source_longitude", max_digits=10, decimal_places=7
    )
    latitude = serializers.DecimalField(
        source="source_latitude", max_digits=10, decimal_places=7
    )

    class Meta:
        model = Activity
        fields = (
            "id", "category_slug", "tag_slugs", "cover_id", "cover_url", "title", "starts_at",
            "ends_at", "formation_deadline", "meeting_place_name", "meeting_address",
            "city_code", "city_name", "longitude", "latitude", "capacity",
            "min_participants", "description", "participation_rules",
            "aa_principal_amount", "refund_template_version", "rejection_reason",
        )

    def get_cover_url(self, obj):
        return build_media_url(obj.cover.object_key) if obj.cover_id else None

    def get_tag_slugs(self, obj):
        tags = list(obj.tags.all())
        return [tag.slug for tag in tags] or ([obj.category.slug] if obj.category_id else [])

    def get_category_slug(self, obj):
        tags = self.get_tag_slugs(obj)
        return tags[0] if tags else ""


class ActivityReportCreateSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=ActivityReport.Reason.choices)
    description = serializers.CharField(required=False, allow_blank=True, max_length=1000)


class ActivityReportReceiptSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    reason_label = serializers.CharField(source="get_reason_display")

    class Meta:
        model = ActivityReport
        fields = (
            "case_no", "activity_id", "reason", "reason_label", "description",
            "status", "status_label", "created_at",
        )
