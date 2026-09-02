from datetime import timedelta

from rest_framework import serializers

from mediafiles.services import build_media_url
from locations.tencent import tencent_map
from locations.models import UserAddress
from providers.models import ProviderProfile, ProviderService
from providers.availability import ensure_booking_within_schedule
from providers.presence import get_provider_live_location, operation_rules, provider_is_online

from .models import ProviderOrder, ProviderOrderReview
from .services import build_quote, validate_booking


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


class ProviderOrderReviewInputSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    content = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ProviderOrderReviewSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.nickname", read_only=True)

    class Meta:
        model = ProviderOrderReview
        fields = ("rating", "content", "customer_name", "created_at")


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
            "customer_confirmed_at", "auto_confirmed_at", "review",
        )

    def get_provider_avatar_url(self, obj):
        return build_media_url(obj.provider.user.avatar_object_key)

    def get_contact_phone_masked(self, obj):
        phone = obj.contact_phone
        return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone

    def get_arrival_photo_url(self, obj):
        if not obj.arrival_photo_id:
            return None
        return build_media_url(obj.arrival_photo.object_key, private=True)


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


class ProviderOrderRejectInputSerializer(serializers.Serializer):
    reason = serializers.CharField(min_length=2, max_length=200, trim_whitespace=True)


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
            "verified": service.provider.user.verification_status
            == service.provider.user.VerificationStatus.VERIFIED,
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
        "pricing_snapshot": quote.snapshot,
    }
