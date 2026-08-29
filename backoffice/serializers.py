from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers

from accounts.models import User
from accounts.serializers import UserSerializer
from activities.models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityReport,
    ActivitySettlement,
)
from activities.serializers import ActivityParticipationRefundOrderSerializer
from mediafiles.services import build_media_url
from orders.models import ProviderOrder
from providers.models import ProviderProfile, ServiceCategory
from providers.presence import (
    get_provider_live_location,
    location_expires_at,
    provider_is_online,
)

from .models import (
    AdminAuditLog,
    Organization,
    OrganizationMember,
    ProviderCreditAdjustment,
    ProviderOrderAfterSalesCase,
    ProviderOrderSupportNote,
    UserRiskFlag,
)


def mask_phone(phone):
    return f"{phone[:3]}****{phone[-4:]}" if phone and len(phone) == 11 else phone


class AdminOverviewQuerySerializer(serializers.Serializer):
    days = serializers.ChoiceField(required=False, default=7, choices=(7, 30))


class AdminServiceCategoryQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        default="all",
        choices=("all", "active", "inactive"),
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=50)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminServiceCategorySerializer(serializers.ModelSerializer):
    icon_url = serializers.SerializerMethodField()
    service_count = serializers.IntegerField(read_only=True, default=0)
    active_service_count = serializers.IntegerField(read_only=True, default=0)
    provider_count = serializers.IntegerField(read_only=True, default=0)
    city_codes = serializers.ListField(
        child=serializers.CharField(max_length=20, trim_whitespace=True),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = ServiceCategory
        fields = (
            "id", "name", "slug", "icon_object_key", "icon_url", "city_codes",
            "sort_order", "is_active", "service_count", "active_service_count",
            "provider_count", "created_at", "updated_at",
        )
        read_only_fields = ("id", "icon_url", "created_at", "updated_at")

    def get_icon_url(self, obj):
        return build_media_url(obj.icon_object_key) if obj.icon_object_key else None

    def validate_city_codes(self, value):
        normalized = []
        for code in value:
            code = code.strip()
            if code and code not in normalized:
                normalized.append(code)
        return normalized

    def validate_slug(self, value):
        normalized = value.strip().lower()
        queryset = ServiceCategory.objects.filter(slug__iexact=normalized)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("该分类标识已存在。")
        return normalized

    def validate(self, attrs):
        instance = self.instance
        next_slug = attrs.get("slug")
        if (
            instance
            and next_slug
            and next_slug != instance.slug
            and instance.provider_services.exists()
        ):
            raise serializers.ValidationError(
                {"slug": "该分类已有达人服务关联，标识不可修改。"}
            )
        return attrs


class AdminActivityQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=Activity.Status.choices,
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    category = serializers.SlugField(required=False, allow_blank=True, max_length=40)
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminActivityReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=("approve", "reject"))
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, attrs):
        if attrs["decision"] == "reject" and len(attrs.get("reason", "").strip()) < 2:
            raise serializers.ValidationError({"reason": "驳回活动时请填写明确原因。"})
        return attrs


class AdminActivityActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("cancel",))
    reason = serializers.CharField(max_length=500)

    def validate_reason(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError("请填写明确的取消原因。")
        return value.strip()


class AdminActivityCategoryQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False, default="all", choices=("all", "active", "inactive")
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=50)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminActivityCategorySerializer(serializers.ModelSerializer):
    icon_url = serializers.SerializerMethodField()
    activity_count = serializers.IntegerField(read_only=True, default=0)
    active_activity_count = serializers.IntegerField(read_only=True, default=0)
    city_codes = serializers.ListField(
        child=serializers.CharField(max_length=20, trim_whitespace=True),
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = ActivityCategory
        fields = (
            "id", "name", "slug", "icon_object_key", "icon_url", "city_codes",
            "min_capacity", "max_capacity", "min_aa_principal_amount",
            "max_aa_principal_amount", "content_guidance", "sort_order", "is_active",
            "activity_count", "active_activity_count", "created_at", "updated_at",
        )
        read_only_fields = ("id", "icon_url", "created_at", "updated_at")

    def get_icon_url(self, obj):
        return build_media_url(obj.icon_object_key) if obj.icon_object_key else None

    def validate_city_codes(self, value):
        normalized = []
        for code in value:
            code = code.strip()
            if code and code not in normalized:
                normalized.append(code)
        return normalized

    def validate_slug(self, value):
        normalized = value.strip().lower()
        queryset = ActivityCategory.objects.filter(slug__iexact=normalized)
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("该活动分类标识已存在。")
        return normalized

    def validate(self, attrs):
        min_capacity = attrs.get("min_capacity", getattr(self.instance, "min_capacity", 2))
        max_capacity = attrs.get("max_capacity", getattr(self.instance, "max_capacity", 100))
        min_amount = attrs.get(
            "min_aa_principal_amount",
            getattr(self.instance, "min_aa_principal_amount", 1),
        )
        max_amount = attrs.get(
            "max_aa_principal_amount",
            getattr(self.instance, "max_aa_principal_amount", 10_000_000),
        )
        errors = {}
        if min_capacity < 2 or max_capacity > 100 or min_capacity > max_capacity:
            errors["max_capacity"] = "人数范围须在2—100人内，且上限不得小于下限。"
        if min_amount < 1 or max_amount > 10_000_000 or min_amount > max_amount:
            errors["max_aa_principal_amount"] = "AA本金范围无效。"
        if (
            self.instance
            and attrs.get("slug")
            and attrs["slug"] != self.instance.slug
            and self.instance.activities.exists()
        ):
            errors["slug"] = "该分类已有活动关联，标识不可修改。"
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class AdminActivityReportQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False, allow_blank=True, choices=ActivityReport.Status.choices
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminActivityReportActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("start_review", "resolve", "reject"))
    result_note = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate(self, attrs):
        if attrs["action"] in ("resolve", "reject") and len(
            attrs.get("result_note", "").strip()
        ) < 2:
            raise serializers.ValidationError({"result_note": "请填写明确的处理结论。"})
        return attrs


class AdminActivityReportSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    reason_label = serializers.CharField(source="get_reason_display")
    activity_title = serializers.CharField(source="activity.title")
    activity_status = serializers.CharField(source="activity.status")
    city_code = serializers.CharField(source="activity.city_code")
    city_name = serializers.CharField(source="activity.city_name")
    organizer_name = serializers.CharField(source="activity.organizer.nickname")
    reporter_name = serializers.CharField(source="reporter.nickname")
    reporter_phone_masked = serializers.SerializerMethodField()
    reviewed_by_name = serializers.CharField(source="reviewed_by.nickname", allow_null=True)

    class Meta:
        model = ActivityReport
        fields = (
            "case_no", "activity_id", "activity_title", "activity_status", "city_code",
            "city_name", "organizer_name", "reporter_name", "reporter_phone_masked",
            "reason", "reason_label", "description", "status", "status_label",
            "result_note", "reviewed_by_name", "reviewed_at", "created_at", "updated_at",
        )

    def get_reporter_phone_masked(self, obj):
        return mask_phone(obj.reporter.phone)


class AdminActivityFinanceQuerySerializer(serializers.Serializer):
    record_type = serializers.ChoiceField(
        required=False,
        default="payment",
        choices=("payment", "refund", "after_sales", "settlement"),
    )
    status = serializers.CharField(required=False, allow_blank=True, max_length=24)
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminActivityParticipationPaymentSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    channel_label = serializers.CharField(source="get_channel_display")
    activity_id = serializers.IntegerField(source="participation.activity_id")
    activity_title = serializers.CharField(source="participation.activity.title")
    city_code = serializers.CharField(source="participation.activity.city_code")
    city_name = serializers.CharField(source="participation.activity.city_name")
    payer_name = serializers.CharField(source="payer.nickname")
    payer_phone_masked = serializers.SerializerMethodField()

    class Meta:
        model = ActivityParticipationPaymentOrder
        fields = (
            "order_no", "activity_id", "activity_title", "city_code", "city_name",
            "payer_name", "payer_phone_masked", "aa_principal_amount",
            "platform_service_fee_amount", "payable_amount", "channel", "channel_label",
            "status", "status_label", "gateway_trade_no", "expires_at", "paid_at",
            "closed_at", "created_at", "updated_at",
        )

    def get_payer_phone_masked(self, obj):
        return mask_phone(obj.payer.phone)


class AdminActivityParticipationRefundSerializer(serializers.ModelSerializer):
    refund_type_label = serializers.CharField(source="get_refund_type_display")
    status_label = serializers.CharField(source="get_status_display")
    activity_title = serializers.CharField(source="activity.title")
    city_code = serializers.CharField(source="activity.city_code")
    city_name = serializers.CharField(source="activity.city_name")
    beneficiary_name = serializers.CharField(source="beneficiary.nickname")
    beneficiary_phone_masked = serializers.SerializerMethodField()
    payment_order_no = serializers.CharField(source="payment_order.order_no")
    operator_name = serializers.CharField(source="operator.nickname", allow_null=True)

    class Meta:
        model = ActivityParticipationRefundOrder
        fields = (
            "refund_no", "payment_order_no", "activity_id", "activity_title",
            "city_code", "city_name", "beneficiary_name", "beneficiary_phone_masked",
            "refund_type", "refund_type_label", "status", "status_label",
            "principal_refund_amount", "service_fee_refund_amount", "refund_amount",
            "retained_principal_amount", "retained_service_fee_amount",
            "retained_principal_destination", "reason", "operator_name",
            "requested_at", "refunded_at", "created_at", "updated_at",
        )

    def get_beneficiary_phone_masked(self, obj):
        return mask_phone(obj.beneficiary.phone)


class AdminActivityAfterSalesSerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source="get_reason_display")
    status_label = serializers.CharField(source="get_status_display")
    activity_id = serializers.IntegerField(source="participation.activity_id")
    activity_title = serializers.CharField(source="participation.activity.title")
    city_code = serializers.CharField(source="participation.activity.city_code")
    city_name = serializers.CharField(source="participation.activity.city_name")
    applicant_name = serializers.CharField(source="applicant.nickname")
    applicant_phone_masked = serializers.SerializerMethodField()
    reviewed_by_name = serializers.CharField(source="reviewed_by.nickname", allow_null=True)
    refund_order = ActivityParticipationRefundOrderSerializer(read_only=True)
    evidence_count = serializers.SerializerMethodField()

    class Meta:
        model = ActivityAfterSalesCase
        fields = (
            "case_no", "activity_id", "activity_title", "city_code", "city_name",
            "applicant_name", "applicant_phone_masked", "reason", "reason_label",
            "description", "evidence_count", "status", "status_label",
            "requested_principal_amount", "requested_service_fee_amount",
            "requested_amount", "approved_principal_amount",
            "approved_service_fee_amount", "approved_amount", "result_note",
            "reviewed_by_name", "reviewed_at", "refund_order", "created_at", "updated_at",
        )

    def get_applicant_phone_masked(self, obj):
        return mask_phone(obj.applicant.phone)

    def get_evidence_count(self, obj):
        return len(obj.evidence_object_keys)


class AdminActivityAfterSalesActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("start_review", "approve", "reject"))
    approved_principal_amount = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    approved_service_fee_amount = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    result_note = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, trim_whitespace=True
    )

    def validate(self, attrs):
        if attrs["action"] in ("approve", "reject") and len(
            attrs.get("result_note", "")
        ) < 5:
            raise serializers.ValidationError({"result_note": "处理结论至少填写5个字。"})
        if (
            attrs["action"] == "approve"
            and attrs.get("approved_principal_amount") == 0
            and attrs.get("approved_service_fee_amount") == 0
        ):
            raise serializers.ValidationError("批准退款金额不能为0。")
        return attrs


class AdminActivitySettlementSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    dispute_source_label = serializers.CharField(source="get_dispute_source_display")
    activity_id = serializers.IntegerField(source="activity.id")
    activity_title = serializers.CharField(source="activity.title")
    city_code = serializers.CharField(source="activity.city_code")
    city_name = serializers.CharField(source="activity.city_name")
    beneficiary_name = serializers.CharField(source="beneficiary.nickname")
    beneficiary_phone_masked = serializers.SerializerMethodField()
    available_balance_amount = serializers.SerializerMethodField()

    class Meta:
        model = ActivitySettlement
        fields = (
            "settlement_no", "activity_id", "activity_title", "city_code", "city_name",
            "beneficiary_name", "beneficiary_phone_masked", "status", "status_label",
            "organizer_principal_amount", "participant_principal_amount",
            "retained_participant_principal_amount", "settlement_amount",
            "platform_service_fee_amount", "available_balance_amount",
            "confirmation_started_at", "confirmation_deadline", "risk_frozen_at",
            "freeze_until", "dispute_source", "dispute_source_label", "dispute_reason",
            "calculation_snapshot", "settled_at", "created_at", "updated_at",
        )

    def get_beneficiary_phone_masked(self, obj):
        return mask_phone(obj.beneficiary.phone)

    def get_available_balance_amount(self, obj):
        return ActivitySettlement.objects.filter(
            beneficiary=obj.beneficiary,
            status=ActivitySettlement.Status.SETTLED,
        ).aggregate(total=Sum("settlement_amount"))["total"] or 0


class AdminActivitySettlementActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=("freeze_dispute", "release_dispute", "retry_settlement")
    )
    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, trim_whitespace=True
    )

    def validate(self, attrs):
        if attrs["action"] == "freeze_dispute" and len(attrs.get("reason", "")) < 5:
            raise serializers.ValidationError({"reason": "冻结原因至少填写5个字。"})
        return attrs


class AdminActivitySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    category_name = serializers.CharField(source="category.name")
    category_slug = serializers.CharField(source="category.slug")
    organizer_public_id = serializers.UUIDField(source="organizer.public_id")
    organizer_name = serializers.CharField(source="organizer.nickname")
    organizer_phone_masked = serializers.SerializerMethodField()
    organizer_verification_status = serializers.CharField(source="organizer.verification_status")
    organizer_verification_status_label = serializers.CharField(
        source="organizer.get_verification_status_display"
    )
    organizer_account_status = serializers.CharField(source="organizer.account_status")
    organizer_account_status_label = serializers.CharField(
        source="organizer.get_account_status_display"
    )
    cover_url = serializers.SerializerMethodField()
    participant_count = serializers.IntegerField(read_only=True, default=0)
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.nickname",
        allow_null=True,
    )
    publish_order = serializers.SerializerMethodField()
    participants = serializers.SerializerMethodField()
    refund_records = serializers.SerializerMethodField()
    settlement = serializers.SerializerMethodField()
    report_count = serializers.IntegerField(read_only=True, default=0)
    cancelled_by_name = serializers.CharField(source="cancelled_by.nickname", allow_null=True)

    class Meta:
        model = Activity
        fields = (
            "id", "title", "status", "status_label", "category_name", "category_slug",
            "organizer_public_id", "organizer_name", "organizer_phone_masked",
            "organizer_verification_status", "organizer_verification_status_label",
            "organizer_account_status", "organizer_account_status_label", "cover_url",
            "city_code", "city_name", "starts_at", "ends_at", "formation_deadline",
            "meeting_place_name", "meeting_address", "source_longitude", "source_latitude",
            "capacity", "min_participants", "participant_count", "description",
            "participation_rules", "aa_principal_amount", "refund_template_version",
            "refund_rule_snapshot", "publish_order", "published_at", "reviewed_by_name",
            "reviewed_at", "rejection_reason", "participants", "created_at", "updated_at",
            "cancellation_reason", "cancelled_by_name", "cancelled_at", "refund_records",
            "report_count", "settlement",
        )

    def get_organizer_phone_masked(self, obj):
        return mask_phone(obj.organizer.phone)

    def get_cover_url(self, obj):
        return build_media_url(obj.cover.object_key) if obj.cover_id else None

    def get_publish_order(self, obj):
        order = next(iter(obj.publish_orders.all()), None)
        if not order:
            return None
        return {
            "order_no": order.order_no,
            "status": order.status,
            "status_label": order.get_status_display(),
            "aa_principal_amount": order.aa_principal_amount,
            "platform_service_fee_amount": order.platform_service_fee_amount,
            "payable_amount": order.payable_amount,
            "pricing_snapshot": order.pricing_snapshot,
            "paid_at": order.paid_at,
        }

    def get_participants(self, obj):
        if not self.context.get("include_detail"):
            return []
        return [
            {
                "public_id": participation.user.public_id,
                "nickname": participation.user.nickname,
                "phone_masked": mask_phone(participation.user.phone),
                "status": participation.status,
                "status_label": participation.get_status_display(),
                "joined_at": participation.joined_at,
                "cancelled_at": participation.cancelled_at,
                "payment_expires_at": participation.payment_expires_at,
                "payable_amount": participation.payable_amount,
                "payment_orders": [
                    {
                        "order_no": order.order_no,
                        "status": order.status,
                        "status_label": order.get_status_display(),
                        "channel": order.channel,
                        "channel_label": order.get_channel_display(),
                        "payable_amount": order.payable_amount,
                        "expires_at": order.expires_at,
                        "paid_at": order.paid_at,
                    }
                    for order in participation.payment_orders.all()
                ],
                "refund_orders": ActivityParticipationRefundOrderSerializer(
                    participation.refund_orders.all(), many=True
                ).data,
                "after_sales_cases": [
                    {
                        "case_no": case.case_no,
                        "status": case.status,
                        "status_label": case.get_status_display(),
                        "reason_label": case.get_reason_display(),
                    }
                    for case in participation.after_sales_cases.all()
                ],
            }
            for participation in obj.participations.all()
        ]

    def get_refund_records(self, obj):
        if not self.context.get("include_detail"):
            return []
        return [
            {
                "refund_no": refund.refund_no,
                "refund_type": refund.refund_type,
                "refund_type_label": refund.get_refund_type_display(),
                "status": refund.status,
                "status_label": refund.get_status_display(),
                "principal_amount": refund.principal_amount,
                "service_fee_amount": refund.service_fee_amount,
                "refund_amount": refund.refund_amount,
                "retained_principal_amount": refund.retained_principal_amount,
                "retained_service_fee_amount": refund.retained_service_fee_amount,
                "retained_principal_destination": refund.retained_principal_destination,
                "beneficiary_name": refund.beneficiary.nickname,
                "reason": refund.reason,
                "operator_name": refund.operator.nickname if refund.operator else None,
                "refunded_at": refund.refunded_at,
            }
            for refund in obj.refund_records.all()
        ]

    def get_settlement(self, obj):
        settlement = getattr(obj, "settlement", None)
        return AdminActivitySettlementSerializer(settlement).data if settlement else None


class AdminOrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ("id", "name", "code", "organization_type", "city_codes")


class AdminMeSerializer(serializers.Serializer):
    user = UserSerializer()
    organization = AdminOrganizationSerializer(allow_null=True)
    role_name = serializers.CharField()
    permissions = serializers.ListField(child=serializers.CharField())
    data_scope = serializers.CharField()
    city_codes = serializers.ListField(child=serializers.CharField())


class ProviderReviewListSerializer(serializers.ModelSerializer):
    public_id = serializers.UUIDField(source="user.public_id")
    nickname = serializers.CharField(source="user.nickname")
    phone = serializers.CharField(source="user.phone")
    verification_status = serializers.CharField(source="user.verification_status")
    gender = serializers.CharField(source="user.gender")
    birth_date = serializers.DateField(source="user.birth_date", allow_null=True)
    lifestyle_photo_url = serializers.SerializerMethodField()
    service_names = serializers.SerializerMethodField()

    class Meta:
        model = ProviderProfile
        fields = (
            "id", "public_id", "nickname", "phone", "verification_status", "gender",
            "birth_date", "status", "lifestyle_photo_url", "service_city_code",
            "service_city_name", "bio", "max_service_radius_km",
            "service_names", "submitted_at", "reviewed_at", "rejection_reason",
        )

    def get_service_names(self, obj):
        return [service.category.name for service in obj.services.all()]

    def get_lifestyle_photo_url(self, obj):
        if not obj.lifestyle_photo_id:
            return None
        return build_media_url(obj.lifestyle_photo.object_key)


class ProviderApplicationQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        default=ProviderProfile.Status.PENDING,
        choices=ProviderProfile.Status.choices,
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    verification_status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=User.VerificationStatus.choices,
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=50)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=10, min_value=1, max_value=50)


class ProviderReviewDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=("approve", "reject"))
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, attrs):
        if attrs["decision"] == "reject" and not attrs.get("reason", "").strip():
            raise serializers.ValidationError({"reason": "驳回申请时必须填写原因。"})
        return attrs


class AdminUserQuerySerializer(serializers.Serializer):
    verification_status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=User.VerificationStatus.choices,
    )
    account_status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=User.AccountStatus.choices,
    )
    identity = serializers.ChoiceField(
        required=False,
        default="all",
        choices=("all", "provider", "user"),
    )
    risk = serializers.ChoiceField(
        required=False,
        default="all",
        choices=("all", "flagged", "unflagged"),
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=50)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminUserAccountActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("restrict", "suspend", "restore"))
    reason = serializers.CharField(min_length=2, max_length=500, trim_whitespace=True)


class AdminUserRiskActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("mark", "clear"))
    level = serializers.ChoiceField(
        choices=UserRiskFlag.Level.choices,
        required=False,
    )
    reason = serializers.CharField(min_length=2, max_length=500, trim_whitespace=True)

    def validate(self, attrs):
        if attrs["action"] == "mark" and "level" not in attrs:
            raise serializers.ValidationError({"level": "标记风险用户时请选择风险等级。"})
        return attrs


class AdminUserRiskFlagSerializer(serializers.ModelSerializer):
    level_label = serializers.CharField(source="get_level_display")
    marked_by_name = serializers.CharField(source="marked_by.nickname")
    cleared_by_name = serializers.CharField(
        source="cleared_by.nickname", allow_null=True
    )

    class Meta:
        model = UserRiskFlag
        fields = (
            "level", "level_label", "reason", "is_active", "marked_by_name",
            "marked_at", "cleared_by_name", "cleared_at", "updated_at",
        )


class AdminUserListSerializer(serializers.ModelSerializer):
    phone_masked = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    gender_label = serializers.CharField(source="get_gender_display")
    verification_status_label = serializers.CharField(source="get_verification_status_display")
    account_status_label = serializers.CharField(source="get_account_status_display")
    identity = serializers.SerializerMethodField()
    provider_status = serializers.SerializerMethodField()
    provider_status_label = serializers.SerializerMethodField()
    risk_flag = serializers.SerializerMethodField()
    order_count = serializers.IntegerField(read_only=True, default=0)
    activity_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = User
        fields = (
            "public_id", "nickname", "phone_masked", "avatar_url", "gender",
            "gender_label", "birth_date", "verification_status",
            "verification_status_label", "account_status", "account_status_label",
            "identity", "provider_status", "provider_status_label", "risk_flag",
            "order_count", "activity_count", "date_joined", "last_login",
        )

    def get_phone_masked(self, obj):
        return mask_phone(obj.phone)

    def get_avatar_url(self, obj):
        return build_media_url(obj.avatar_object_key)

    def get_identity(self, obj):
        return "provider" if hasattr(obj, "provider_profile") else "user"

    def get_provider_status(self, obj):
        profile = getattr(obj, "provider_profile", None)
        return profile.status if profile else None

    def get_provider_status_label(self, obj):
        profile = getattr(obj, "provider_profile", None)
        return profile.get_status_display() if profile else None

    def get_risk_flag(self, obj):
        flag = getattr(obj, "admin_risk_flag", None)
        if not flag or not flag.is_active:
            return None
        return AdminUserRiskFlagSerializer(flag).data


class ProviderAdminQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ProviderProfile.Status.choices,
    )
    accepting = serializers.ChoiceField(
        required=False,
        default="all",
        choices=("all", "accepting", "paused", "restricted"),
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    verification_status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=User.VerificationStatus.choices,
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=50)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class ProviderAdminActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=("restrict_orders", "resume_orders", "suspend_qualification", "restore_qualification")
    )
    reason = serializers.CharField(min_length=2, max_length=500, trim_whitespace=True)


class ProviderCreditAdjustmentInputSerializer(serializers.Serializer):
    delta = serializers.IntegerField(min_value=-100, max_value=100)
    reason = serializers.CharField(min_length=2, max_length=500, trim_whitespace=True)

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("调整分值不能为 0。")
        return value


class ProviderCreditAdjustmentSerializer(serializers.ModelSerializer):
    operator_name = serializers.CharField(source="operator.nickname")
    organization_name = serializers.CharField(source="organization.name", allow_null=True)

    class Meta:
        model = ProviderCreditAdjustment
        fields = (
            "id", "delta", "before_score", "after_score", "reason",
            "operator_name", "organization_name", "created_at",
        )


class ProviderAdminSerializer(serializers.ModelSerializer):
    public_id = serializers.UUIDField(source="user.public_id")
    nickname = serializers.CharField(source="user.nickname")
    phone_masked = serializers.SerializerMethodField()
    gender = serializers.CharField(source="user.gender")
    gender_label = serializers.CharField(source="user.get_gender_display")
    birth_date = serializers.DateField(source="user.birth_date", allow_null=True)
    verification_status = serializers.CharField(source="user.verification_status")
    verification_status_label = serializers.CharField(
        source="user.get_verification_status_display"
    )
    account_status = serializers.CharField(source="user.account_status")
    account_status_label = serializers.CharField(source="user.get_account_status_display")
    status_label = serializers.CharField(source="get_status_display")
    lifestyle_photo_available = serializers.SerializerMethodField()
    lifestyle_photo_url = serializers.SerializerMethodField()
    is_online = serializers.SerializerMethodField()
    has_live_location = serializers.SerializerMethodField()
    current_longitude = serializers.SerializerMethodField()
    current_latitude = serializers.SerializerMethodField()
    location_accuracy_m = serializers.SerializerMethodField()
    location_updated_at = serializers.SerializerMethodField()
    location_expires_at = serializers.SerializerMethodField()
    service_names = serializers.SerializerMethodField()
    services = serializers.SerializerMethodField()
    weekly_availability = serializers.SerializerMethodField()
    credit_adjustments = serializers.SerializerMethodField()

    class Meta:
        model = ProviderProfile
        fields = (
            "id", "public_id", "nickname", "phone_masked", "gender", "gender_label",
            "birth_date", "verification_status", "verification_status_label",
            "account_status", "account_status_label", "status", "status_label", "bio",
            "lifestyle_photo_available", "lifestyle_photo_url", "service_city_code",
            "service_city_name", "is_online", "has_live_location", "current_longitude",
            "current_latitude", "location_accuracy_m", "location_updated_at",
            "location_expires_at",
            "max_service_radius_km", "rating", "service_count",
            "order_count", "credit_score", "is_accepting_orders", "admin_order_restricted",
            "admin_restriction_reason", "service_names", "services", "weekly_availability",
            "credit_adjustments", "submitted_at", "reviewed_at", "rejection_reason",
            "created_at", "updated_at",
        )

    def get_phone_masked(self, obj):
        return mask_phone(obj.user.phone)

    def get_lifestyle_photo_available(self, obj):
        return bool(obj.lifestyle_photo_id)

    def get_lifestyle_photo_url(self, obj):
        if not obj.lifestyle_photo_id or not self.context.get("can_review", False):
            return None
        return build_media_url(obj.lifestyle_photo.object_key)

    def get_is_online(self, obj):
        return provider_is_online(obj)

    def get_has_live_location(self, obj):
        return get_provider_live_location(obj) is not None

    def get_current_longitude(self, obj):
        location = get_provider_live_location(obj)
        return str(location.source_longitude) if location else None

    def get_current_latitude(self, obj):
        location = get_provider_live_location(obj)
        return str(location.source_latitude) if location else None

    def get_location_accuracy_m(self, obj):
        location = get_provider_live_location(obj)
        return str(location.accuracy_m) if location else None

    def get_location_updated_at(self, obj):
        location = get_provider_live_location(obj)
        return location.received_at if location else None

    def get_location_expires_at(self, obj):
        return location_expires_at(get_provider_live_location(obj))

    def get_service_names(self, obj):
        return [service.category.name for service in obj.services.all() if service.is_active]

    def get_services(self, obj):
        if not self.context.get("include_detail", False):
            return []
        return [
            {
                "id": service.id,
                "category": service.category.name,
                "billing_type": service.billing_type,
                "billing_type_label": service.get_billing_type_display(),
                "price_amount": service.price_amount,
                "estimated_duration_minutes": service.estimated_duration_minutes,
                "description": service.description,
                "is_active": service.is_active,
            }
            for service in obj.services.all()
        ]

    def get_weekly_availability(self, obj):
        if not self.context.get("include_detail", False):
            return []
        return [
            {
                "weekday": item.weekday,
                "weekday_label": item.get_weekday_display(),
                "starts_at": item.starts_at,
                "ends_at": item.ends_at,
                "is_active": item.is_active,
            }
            for item in obj.weekly_availability.all()
        ]

    def get_credit_adjustments(self, obj):
        if not self.context.get("include_detail", False):
            return []
        return ProviderCreditAdjustmentSerializer(
            obj.admin_credit_adjustments.all(), many=True
        ).data


EVIDENCE_REQUIRED_STATUSES = frozenset(
    (
        ProviderOrder.Status.IN_SERVICE,
        ProviderOrder.Status.PENDING_CONFIRMATION,
        ProviderOrder.Status.PENDING_REVIEW,
        ProviderOrder.Status.COMPLETED,
    )
)
DEPARTURE_REQUIRED_STATUSES = EVIDENCE_REQUIRED_STATUSES | {ProviderOrder.Status.DEPARTED}
SERVICE_START_REQUIRED_STATUSES = EVIDENCE_REQUIRED_STATUSES
COMPLETION_REQUIRED_STATUSES = frozenset(
    (
        ProviderOrder.Status.PENDING_CONFIRMATION,
        ProviderOrder.Status.PENDING_REVIEW,
        ProviderOrder.Status.COMPLETED,
    )
)
CUSTOMER_CONFIRMATION_REQUIRED_STATUSES = frozenset(
    (ProviderOrder.Status.PENDING_REVIEW, ProviderOrder.Status.COMPLETED)
)


def provider_order_anomalies(order):
    anomalies = []
    if order.status in EVIDENCE_REQUIRED_STATUSES and not order.arrival_photo_id:
        anomalies.append({"code": "missing_evidence", "label": "缺少集合照"})
    timeline_gap = (
        (order.status in DEPARTURE_REQUIRED_STATUSES and not order.departed_at)
        or (order.status in SERVICE_START_REQUIRED_STATUSES and not order.service_started_at)
        or (order.status in COMPLETION_REQUIRED_STATUSES and not order.completion_submitted_at)
        or (
            order.status in CUSTOMER_CONFIRMATION_REQUIRED_STATUSES
            and not order.customer_confirmed_at
        )
    )
    if timeline_gap:
        anomalies.append({"code": "timeline_gap", "label": "履约时间线缺失"})
    overdue_before = timezone.now() - timedelta(
        days=settings.PROVIDER_ORDER_AUTO_CONFIRM_DAYS
    )
    if (
        order.status == ProviderOrder.Status.PENDING_CONFIRMATION
        and order.completion_submitted_at
        and order.completion_submitted_at <= overdue_before
    ):
        anomalies.append({"code": "confirmation_overdue", "label": "待确认超时"})
    return anomalies


class ProviderOrderAdminQuerySerializer(serializers.Serializer):
    stage = serializers.ChoiceField(
        required=False,
        default="all",
        choices=("all", "active", "pending_confirmation", "ended"),
    )
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ProviderOrder.Status.choices,
    )
    anomaly = serializers.ChoiceField(
        required=False,
        default="all",
        choices=(
            "all", "any", "missing_evidence", "timeline_gap", "confirmation_overdue",
        ),
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class ProviderOrderSupportNoteInputSerializer(serializers.Serializer):
    content = serializers.CharField(min_length=1, max_length=1000, trim_whitespace=True)


class ProviderOrderSupportNoteSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.nickname")
    organization_name = serializers.CharField(source="organization.name", allow_null=True)

    class Meta:
        model = ProviderOrderSupportNote
        fields = ("id", "author_name", "organization_name", "content", "created_at")


class ProviderOrderAfterSalesCaseQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ProviderOrderAfterSalesCase.Status.choices,
    )
    case_type = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ProviderOrderAfterSalesCase.CaseType.choices,
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class ProviderOrderAfterSalesCaseCreateSerializer(serializers.Serializer):
    order_no = serializers.CharField(max_length=32)
    case_type = serializers.ChoiceField(choices=ProviderOrderAfterSalesCase.CaseType.choices)
    requested_amount = serializers.IntegerField(min_value=0)
    reason = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True)


class ProviderOrderAfterSalesCaseActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("start_review", "approve", "reject"))
    approved_amount = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    result_note = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, trim_whitespace=True
    )

    def validate(self, attrs):
        if attrs["action"] in ("approve", "reject") and len(attrs.get("result_note", "")) < 5:
            raise serializers.ValidationError({"result_note": "审核结论至少填写 5 个字。"})
        return attrs


class ProviderOrderAfterSalesCaseSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    case_type_label = serializers.CharField(source="get_case_type_display")
    order_no = serializers.CharField(source="order.order_no")
    order_status = serializers.CharField(source="order.status")
    order_status_label = serializers.CharField(source="order.get_status_display")
    order_payable_amount = serializers.IntegerField(source="order.payable_amount")
    customer_name = serializers.CharField(source="order.customer.nickname")
    provider_name = serializers.CharField(source="order.provider_name_snapshot")
    service_name = serializers.CharField(source="order.service_name_snapshot")
    service_city_code = serializers.CharField(source="order.provider.service_city_code")
    service_city_name = serializers.CharField(source="order.provider.service_city_name")
    creator_name = serializers.CharField(source="creator.nickname")
    organization_name = serializers.CharField(source="organization.name", allow_null=True)
    reviewed_by_name = serializers.CharField(
        source="reviewed_by.nickname", allow_null=True
    )

    class Meta:
        model = ProviderOrderAfterSalesCase
        fields = (
            "public_id", "case_no", "case_type", "case_type_label", "status",
            "status_label", "order_no", "order_status", "order_status_label",
            "order_payable_amount", "customer_name", "provider_name", "service_name",
            "service_city_code", "service_city_name", "requested_amount",
            "approved_amount", "reason", "result_note", "creator_name",
            "organization_name", "reviewed_by_name", "reviewed_at", "created_at",
            "updated_at",
        )


class ProviderOrderAdminSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    customer_public_id = serializers.UUIDField(source="customer.public_id")
    customer_name = serializers.CharField(source="customer.nickname")
    customer_phone_masked = serializers.SerializerMethodField()
    provider_public_id = serializers.UUIDField(source="provider.user.public_id")
    provider_name = serializers.CharField(source="provider_name_snapshot")
    provider_phone_masked = serializers.SerializerMethodField()
    service_name = serializers.CharField(source="service_name_snapshot")
    service_city_code = serializers.CharField(source="provider.service_city_code")
    service_city_name = serializers.CharField(source="provider.service_city_name")
    contact_phone_masked = serializers.SerializerMethodField()
    contact_gender_label = serializers.CharField(source="get_contact_gender_display")
    arrival_photo_available = serializers.SerializerMethodField()
    arrival_location = serializers.SerializerMethodField()
    anomalies = serializers.SerializerMethodField()
    support_notes = ProviderOrderSupportNoteSerializer(many=True, read_only=True)
    after_sales_cases = ProviderOrderAfterSalesCaseSerializer(many=True, read_only=True)

    class Meta:
        model = ProviderOrder
        fields = (
            "public_id", "order_no", "status", "status_label", "customer_public_id",
            "customer_name", "customer_phone_masked", "provider_public_id", "provider_name",
            "provider_phone_masked", "service_name", "service_city_code", "service_city_name",
            "starts_at", "ends_at", "duration_minutes", "meeting_location_name",
            "meeting_address", "contact_name", "contact_gender", "contact_gender_label",
            "contact_phone_masked", "note", "unit_price_amount", "service_fee_amount",
            "transport_fee_amount", "other_fee_amount", "discount_amount",
            "payable_amount", "paid_at", "accepted_at",
            "departed_at", "arrival_photo_available", "arrival_photo_uploaded_at",
            "arrival_location", "service_started_at", "completion_submitted_at",
            "customer_confirmed_at", "cancelled_at", "created_at", "updated_at",
            "anomalies", "support_notes", "after_sales_cases",
        )

    @staticmethod
    def mask_phone(phone):
        return f"{phone[:3]}****{phone[-4:]}" if phone and len(phone) == 11 else phone

    def get_customer_phone_masked(self, obj):
        return self.mask_phone(obj.customer.phone)

    def get_provider_phone_masked(self, obj):
        return self.mask_phone(obj.provider.user.phone)

    def get_contact_phone_masked(self, obj):
        return self.mask_phone(obj.contact_phone)

    def get_arrival_photo_available(self, obj):
        return bool(obj.arrival_photo_id)

    def get_arrival_location(self, obj):
        if obj.arrival_longitude is None or obj.arrival_latitude is None:
            return None
        return {
            "longitude": obj.arrival_longitude,
            "latitude": obj.arrival_latitude,
            "accuracy_m": obj.arrival_location_accuracy_m,
        }

    def get_anomalies(self, obj):
        return provider_order_anomalies(obj)


class OrganizationMemberSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source="user.nickname", read_only=True)
    phone = serializers.CharField(source="user.phone", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)

    class Meta:
        model = OrganizationMember
        fields = (
            "id", "user", "user_name", "phone", "organization", "organization_name",
            "role", "role_name", "city_codes", "is_active", "created_at",
        )


class AuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source="actor.nickname", read_only=True)

    class Meta:
        model = AdminAuditLog
        fields = (
            "id", "actor_name", "action", "target_type", "target_id", "before", "after",
            "ip_address", "created_at",
        )
