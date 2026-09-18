from datetime import timedelta

from rest_framework import serializers

from backoffice.models import ProviderOrderAfterSalesCase
from mediafiles.models import MediaAsset
from mediafiles.services import build_media_url
from locations.tencent import tencent_map
from locations.models import UserAddress
from providers.models import ProviderProfile, ProviderService
from providers.availability import ensure_booking_within_schedule
from providers.presence import get_provider_live_location, operation_rules, provider_is_online

from .models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderReview,
    ProviderOrderSettlement,
)
from .services import build_quote, validate_booking


def public_pricing_snapshot(snapshot: dict) -> dict:
    """Keep customer-facing pricing rules while omitting internal revenue terms."""
    return {
        key: value
        for key, value in snapshot.items()
        if key != "platform_commission_rate"
    }


class ProviderOrderInputSerializer(serializers.Serializer):
    service_id = serializers.IntegerField(min_value=1)
    starts_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(min_value=30, max_value=480)
    address_id = serializers.IntegerField(min_value=1)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, attrs):
        request = self.context.get("request")
        if request is None:
            raise serializers.ValidationError({"address_id": "缺少当前用户信息。"})
        try:
            address = UserAddress.objects.get(id=attrs["address_id"], user=request.user)
        except UserAddress.DoesNotExist as exc:
            raise serializers.ValidationError({"address_id": "常用地址不存在。"}) from exc
        if not (
            address.contact_name.strip()
            and address.contact_gender in UserAddress.ContactGender.values
            and address.contact_phone.isdigit()
            and len(address.contact_phone) == 11
        ):
            raise serializers.ValidationError({"address_id": "请先补全该地址的联系人信息。"})
        try:
            service = ProviderService.objects.select_related(
                "provider__user", "provider__live_location", "category"
            ).get(
                id=attrs["service_id"],
                is_active=True,
                category__is_active=True,
                provider__status=ProviderProfile.Status.APPROVED,
                provider__identity_status=ProviderProfile.IdentityStatus.VERIFIED,
                provider__is_accepting_orders=True,
            )
        except ProviderService.DoesNotExist as exc:
            raise serializers.ValidationError({"service_id": "服务不存在或不可预约。"}) from exc
        duration, ends_at = validate_booking(
            service, attrs["starts_at"], attrs["duration_minutes"]
        )
        ensure_booking_within_schedule(service.provider, attrs["starts_at"], ends_at)
        provider = service.provider
        location = get_provider_live_location(provider)
        if not provider_is_online(provider) or location is None:
            raise serializers.ValidationError({"service_id": "达人当前不在线，暂时无法预约。"})
        route = tencent_map.driving_route(
            location.source_longitude,
            location.source_latitude,
            address.longitude,
            address.latitude,
        )
        if route.distance_km > provider.max_service_radius_km:
            raise serializers.ValidationError(
                {"address_id": f"该地点距达人约{route.distance_km}公里，超出{provider.max_service_radius_km}公里服务范围。"}
            )
        attrs["service"] = service
        attrs["address"] = address
        attrs["meeting_location_name"] = address.name
        attrs["meeting_address"] = address.address
        attrs["longitude"] = address.longitude
        attrs["latitude"] = address.latitude
        attrs["contact_name"] = address.contact_name
        attrs["contact_gender"] = address.contact_gender
        attrs["contact_phone"] = address.contact_phone
        attrs["duration_minutes"] = duration
        attrs["ends_at"] = ends_at
        attrs["route"] = route
        attrs["quote"] = build_quote(service, duration, route.distance_km)
        return attrs


class ProviderOrderPaymentSessionInputSerializer(serializers.Serializer):
    payment_scene = serializers.ChoiceField(
        choices=("official_account", "mobile_app"),
        default="official_account",
        required=False,
    )


class ProviderOrderReviewInputSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    content = serializers.CharField(required=False, allow_blank=True, max_length=500)
    image_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, max_length=3, default=list
    )
    is_anonymous = serializers.BooleanField(required=False, default=False)


class ProviderOrderReviewListQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=50)


class ProviderOrderReviewSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    image_urls = serializers.SerializerMethodField()

    class Meta:
        model = ProviderOrderReview
        fields = (
            "rating", "content", "customer_name", "image_urls", "is_anonymous",
            "created_at",
        )

    def get_customer_name(self, obj):
        return "匿名用户" if obj.is_anonymous else obj.customer.nickname

    def get_image_urls(self, obj):
        return [build_media_url(image.object_key) for image in obj.images.all()]


class MyProviderOrderReviewSerializer(ProviderOrderReviewSerializer):
    order_no = serializers.CharField(source="order.order_no")
    provider_public_id = serializers.UUIDField(source="provider.user.public_id")
    provider_name = serializers.CharField(source="order.provider_name_snapshot")
    service_name = serializers.CharField(source="order.service_name_snapshot")

    class Meta(ProviderOrderReviewSerializer.Meta):
        fields = ProviderOrderReviewSerializer.Meta.fields + (
            "order_no", "provider_public_id", "provider_name", "service_name", "is_visible",
        )


class ProviderOrderPaymentSummarySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    channel_label = serializers.CharField(source="get_channel_display")

    class Meta:
        model = ProviderOrderPaymentOrder
        fields = (
            "payment_no", "channel", "channel_label", "status", "status_label",
            "payable_amount", "paid_at", "closed_at",
        )


class ProviderOrderRefundSummarySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")

    class Meta:
        model = ProviderOrderRefundOrder
        fields = (
            "refund_no", "status", "status_label", "refund_amount", "reason",
            "requested_at", "refunded_at",
        )


class ProviderOrderSettlementSummarySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")

    class Meta:
        model = ProviderOrderSettlement
        fields = (
            "settlement_no", "status", "status_label", "platform_commission_amount",
            "provider_settlement_amount", "freeze_until", "settled_at",
        )


class ProviderOrderAfterSalesInputSerializer(serializers.Serializer):
    case_type = serializers.ChoiceField(
        choices=(
            ProviderOrderAfterSalesCase.CaseType.REFUND,
            ProviderOrderAfterSalesCase.CaseType.SERVICE_DISPUTE,
            ProviderOrderAfterSalesCase.CaseType.OTHER,
        )
    )
    requested_amount = serializers.IntegerField(
        min_value=1, error_messages={"min_value": "申请退款金额必须大于 0。"}
    )
    reason = serializers.CharField(min_length=5, max_length=1000, trim_whitespace=True)
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

class ProviderOrderAfterSalesSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    case_type_label = serializers.CharField(source="get_case_type_display")
    evidence_urls = serializers.SerializerMethodField()
    refund_order = serializers.SerializerMethodField()

    class Meta:
        model = ProviderOrderAfterSalesCase
        fields = (
            "case_no", "case_type", "case_type_label", "status", "status_label",
            "requested_amount", "approved_amount", "reason", "evidence_urls",
            "result_note", "reviewed_at", "refund_order", "created_at", "updated_at",
        )

    def get_evidence_urls(self, obj):
        return [
            build_media_url(object_key, private=True)
            for object_key in obj.evidence_object_keys
        ]

    def get_refund_order(self, obj):
        refund = next(
            (
                item
                for item in obj.order.refund_orders.all()
                if item.source_reference == obj.case_no
            ),
            None,
        )
        return ProviderOrderRefundSummarySerializer(refund).data if refund else None


class ProviderOrderSerializer(serializers.ModelSerializer):
    provider_public_id = serializers.UUIDField(source="provider.user.public_id")
    provider_name = serializers.CharField(source="provider_name_snapshot")
    provider_avatar_url = serializers.SerializerMethodField()
    service_name = serializers.CharField(source="service_name_snapshot")
    status_label = serializers.CharField(source="get_status_display")
    contact_gender_label = serializers.CharField(source="get_contact_gender_display")
    contact_phone_masked = serializers.SerializerMethodField()
    arrival_photo_url = serializers.SerializerMethodField()
    review = ProviderOrderReviewSerializer(read_only=True, allow_null=True)
    payment_order = ProviderOrderPaymentSummarySerializer(read_only=True, allow_null=True)
    refund_orders = ProviderOrderRefundSummarySerializer(many=True, read_only=True)
    settlement = ProviderOrderSettlementSummarySerializer(read_only=True, allow_null=True)
    after_sales = serializers.SerializerMethodField()
    pricing_snapshot = serializers.SerializerMethodField()

    class Meta:
        model = ProviderOrder
        fields = (
            "public_id", "order_no", "status", "status_label", "provider_public_id",
            "provider_name", "provider_avatar_url", "service_name", "billing_type_snapshot",
            "unit_price_amount", "starts_at", "ends_at", "duration_minutes",
            "meeting_location_name", "meeting_address", "contact_name", "contact_gender",
            "contact_gender_label", "contact_phone_masked", "note", "service_fee_amount",
            "transport_fee_amount", "other_fee_amount", "discount_amount", "payable_amount",
            "pricing_snapshot", "payment_expires_at", "paid_at", "acceptance_expires_at",
            "created_at",
            "accepted_at", "provider_rejected_at", "provider_rejection_reason",
            "departed_at", "arrival_photo_url", "arrival_photo_uploaded_at",
            "service_started_at", "completion_submitted_at", "confirmation_expires_at",
            "customer_confirmed_at", "auto_confirmed_at", "review", "payment_order",
            "refund_orders", "settlement", "after_sales",
        )

    def get_provider_avatar_url(self, obj):
        return build_media_url(obj.provider.user.avatar_object_key)

    def get_pricing_snapshot(self, obj):
        return public_pricing_snapshot(obj.pricing_snapshot)

    def get_contact_phone_masked(self, obj):
        phone = obj.contact_phone
        return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone

    def get_arrival_photo_url(self, obj):
        if not obj.arrival_photo_id:
            return None
        return build_media_url(obj.arrival_photo.object_key, private=True)

    def get_after_sales(self, obj):
        prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("after_sales_cases")
        if prefetched is None:
            case = obj.after_sales_cases.order_by("-created_at", "-id").first()
        else:
            case = prefetched[0] if prefetched else None
        if not case:
            return None
        refund = next(
            (item for item in obj.refund_orders.all() if item.source_reference == case.case_no),
            None,
        )
        return {
            "case_no": case.case_no,
            "case_type": case.case_type,
            "case_type_label": case.get_case_type_display(),
            "status": case.status,
            "status_label": case.get_status_display(),
            "requested_amount": case.requested_amount,
            "approved_amount": case.approved_amount,
            "result_note": case.result_note,
            "created_at": case.created_at,
            "updated_at": case.updated_at,
            "refund_no": refund.refund_no if refund else None,
            "refund_status": refund.status if refund else None,
            "refund_status_label": refund.get_status_display() if refund else None,
            "refund_amount": refund.refund_amount if refund else None,
            "refunded_at": refund.refunded_at if refund else None,
        }


class ProviderOrderArrivalEvidenceInputSerializer(serializers.Serializer):
    photo_id = serializers.UUIDField()
    longitude = serializers.DecimalField(
        max_digits=10, decimal_places=7, min_value=-180, max_value=180
    )
    latitude = serializers.DecimalField(
        max_digits=10, decimal_places=7, min_value=-90, max_value=90
    )
    accuracy_m = serializers.DecimalField(
        required=False, allow_null=True, max_digits=8, decimal_places=2, min_value=0
    )


class ProviderOrderManageQuerySerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ProviderOrder.Status.choices,
    )


class ProviderOrderManageSerializer(ProviderOrderSerializer):
    customer_name = serializers.CharField(source="customer.nickname")
    acceptance_expires_at = serializers.SerializerMethodField()
    contact_phone_display = serializers.SerializerMethodField()
    meeting_longitude = serializers.SerializerMethodField()
    meeting_latitude = serializers.SerializerMethodField()

    class Meta(ProviderOrderSerializer.Meta):
        fields = ProviderOrderSerializer.Meta.fields + (
            "customer_name",
            "contact_phone_display",
            "meeting_longitude",
            "meeting_latitude",
        )

    def get_acceptance_expires_at(self, obj):
        if obj.acceptance_expires_at:
            return obj.acceptance_expires_at
        if obj.paid_at:
            timeout = operation_rules().get("acceptance_timeout_minutes", 30)
            return obj.paid_at + timedelta(minutes=timeout)
        return None

    def get_contact_phone_display(self, obj):
        if obj.status in (
            ProviderOrder.Status.PENDING_ACCEPTANCE,
            ProviderOrder.Status.PENDING_SUPPORT,
        ):
            return self.get_contact_phone_masked(obj)
        return obj.contact_phone

    def get_meeting_longitude(self, obj):
        return self.get_meeting_coordinate(obj, "source_longitude")

    def get_meeting_latitude(self, obj):
        return self.get_meeting_coordinate(obj, "source_latitude")

    @staticmethod
    def get_meeting_coordinate(obj, field):
        if obj.status in (
            ProviderOrder.Status.PENDING_ACCEPTANCE,
            ProviderOrder.Status.PENDING_SUPPORT,
        ):
            return None
        return getattr(obj, field)


def quote_payload(validated_data):
    service = validated_data["service"]
    quote = validated_data["quote"]
    return {
        "provider": {
            "public_id": service.provider.user.public_id,
            "nickname": service.provider.user.nickname,
            "avatar_url": build_media_url(service.provider.user.avatar_object_key),
            "verified": service.provider.identity_status
            == service.provider.IdentityStatus.VERIFIED,
        },
        "service": {
            "id": service.id,
            "name": service.category.name,
            "billing_type": service.billing_type,
            "unit_price_amount": service.price_amount,
        },
        "starts_at": validated_data["starts_at"],
        "ends_at": validated_data["ends_at"],
        "duration_minutes": validated_data["duration_minutes"],
        "meeting_location_name": validated_data["meeting_location_name"],
        "meeting_address": validated_data["meeting_address"],
        "route_distance_km": validated_data["route"].distance_km,
        "route_duration_minutes": validated_data["route"].duration_minutes,
        "service_fee_amount": quote.service_fee_amount,
        "transport_fee_amount": quote.transport_fee_amount,
        "other_fee_amount": quote.other_fee_amount,
        "discount_amount": quote.discount_amount,
        "payable_amount": quote.payable_amount,
        "pricing_snapshot": public_pricing_snapshot(quote.snapshot),
    }
