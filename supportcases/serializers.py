from rest_framework import serializers

from mediafiles.services import build_media_url

from .models import SupportCase, SupportCaseRecord


class SupportCaseCreateSerializer(serializers.Serializer):
    case_type = serializers.ChoiceField(
        choices=(
            (SupportCase.CaseType.COMPLAINT, "投诉/反馈"),
            (SupportCase.CaseType.REPORT, "举报"),
        )
    )
    target_type = serializers.ChoiceField(choices=SupportCase.TargetType.choices)
    target_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    reason = serializers.ChoiceField(choices=SupportCase.Reason.choices)
    description = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True)
    attachment_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list, max_length=3
    )
    reward_eligible = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        target_type = attrs["target_type"]
        target_id = attrs.get("target_id", "").strip()
        if target_type == SupportCase.TargetType.GENERAL and target_id:
            raise serializers.ValidationError({"target_id": "平台服务工单无需关联对象。"})
        if target_type != SupportCase.TargetType.GENERAL and not target_id:
            raise serializers.ValidationError({"target_id": "请选择要反馈的对象。"})
        if (
            attrs["reason"] == SupportCase.Reason.PAYMENT_REFUND
            and target_type == SupportCase.TargetType.PROVIDER_ORDER
        ):
            raise serializers.ValidationError(
                {"reason": "订单退款请从订单详情进入退款/售后流程。"}
            )
        if attrs["reward_eligible"]:
            if attrs["case_type"] != SupportCase.CaseType.REPORT or target_type != SupportCase.TargetType.PROVIDER_ORDER:
                raise serializers.ValidationError("举报有奖必须关联达人订单。")
            if not attrs["attachment_ids"]:
                raise serializers.ValidationError({"attachment_ids": "举报有奖至少上传一张证据图片。"})
        return attrs


class SupportCaseListQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False, allow_blank=True, choices=SupportCase.Status.choices
    )
    case_type = serializers.ChoiceField(
        required=False, allow_blank=True, choices=SupportCase.CaseType.choices
    )
    target_type = serializers.ChoiceField(
        required=False, allow_blank=True, choices=SupportCase.TargetType.choices
    )
    city_code = serializers.CharField(required=False, allow_blank=True, max_length=20)
    search = serializers.CharField(required=False, allow_blank=True, max_length=80)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class AdminSupportCaseListQuerySerializer(SupportCaseListQuerySerializer):
    status_group = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=(("terminal", "已完结"),),
    )


class SupportCaseReplySerializer(serializers.Serializer):
    content = serializers.CharField(min_length=2, max_length=1000, trim_whitespace=True)


class SupportCaseReviewRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True)


class AdminSupportCaseActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=("start_review", "resolve", "reject", "close"))
    result_note = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, trim_whitespace=True,
        default="",
    )

    def validate(self, attrs):
        if attrs["action"] in ("resolve", "reject", "close"):
            if len(attrs.get("result_note", "")) < 5:
                raise serializers.ValidationError({"result_note": "处理结论至少填写5个字。"})
        return attrs


class SupportCaseRecordSerializer(serializers.ModelSerializer):
    record_type_label = serializers.CharField(source="get_record_type_display")
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = SupportCaseRecord
        fields = (
            "id", "record_type", "record_type_label", "actor_name", "content",
            "from_status", "to_status", "created_at",
        )

    def get_actor_name(self, obj):
        if not obj.actor_id:
            return "系统"
        if obj.actor_id == obj.case.reporter_id:
            return obj.actor.nickname or "用户"
        return obj.actor.nickname or "平台客服"


class SupportCaseSerializer(serializers.ModelSerializer):
    case_type_label = serializers.CharField(source="get_case_type_display")
    target_type_label = serializers.CharField(source="get_target_type_display")
    reason_label = serializers.CharField(source="get_reason_display")
    status_label = serializers.CharField(source="get_status_display")
    target_id = serializers.SerializerMethodField()
    target_title = serializers.SerializerMethodField()
    target_subtitle = serializers.SerializerMethodField()
    attachment_urls = serializers.SerializerMethodField()
    assignee_name = serializers.SerializerMethodField()
    records = SupportCaseRecordSerializer(many=True, read_only=True)
    reward_issued = serializers.SerializerMethodField()

    class Meta:
        model = SupportCase
        fields = (
            "public_id", "case_no", "case_type", "case_type_label", "target_type",
            "target_type_label", "target_id", "target_title", "target_subtitle",
            "reason", "reason_label", "description", "attachment_urls", "city_code",
            "city_name", "status", "status_label", "assignee_name", "result_note",
            "resolved_at", "review_requested_at", "review_reason", "records",
            "reward_eligible", "reward_issued",
            "created_at", "updated_at",
        )

    def get_reward_issued(self, obj):
        return bool(obj.reward_coupon_id)

    def get_target_id(self, obj):
        if obj.provider_id:
            return str(obj.provider.user.public_id)
        if obj.provider_order_id:
            return obj.provider_order.order_no
        if obj.activity_id:
            return str(obj.activity_id)
        if obj.review_id:
            return str(obj.review_id)
        return ""

    def get_target_title(self, obj):
        if obj.provider_id:
            return obj.provider.public_display_name or "达人"
        if obj.provider_order_id:
            return obj.provider_order.service_name_snapshot
        if obj.activity_id:
            return obj.activity.title
        if obj.review_id:
            return f"{obj.review.customer.nickname or '用户'}的评价"
        return "平台服务"

    def get_target_subtitle(self, obj):
        if obj.provider_order_id:
            return obj.provider_order.order_no
        if obj.review_id:
            return obj.review.content[:50] or f"{obj.review.rating}星评价"
        return obj.city_name

    def get_attachment_urls(self, obj):
        return [
            build_media_url(asset.object_key, private=True)
            for asset in obj.attachments.all()
        ]

    def get_assignee_name(self, obj):
        if not obj.assignee_id:
            return None
        return obj.assignee.nickname or "平台客服"


class AdminSupportCaseSerializer(SupportCaseSerializer):
    reporter_name = serializers.CharField(source="reporter.nickname")
    reporter_phone = serializers.SerializerMethodField()

    class Meta(SupportCaseSerializer.Meta):
        fields = SupportCaseSerializer.Meta.fields + ("reporter_name", "reporter_phone")

    def get_reporter_phone(self, obj):
        phone = obj.reporter.phone
        return f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else phone
