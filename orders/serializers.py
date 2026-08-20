from rest_framework import serializers

from mediafiles.services import build_media_url
from locations.tencent import tencent_map
from providers.models import ProviderProfile, ProviderService
from providers.availability import ensure_booking_within_schedule

from .models import ProviderOrder
from .services import build_quote, validate_booking


class ProviderOrderInputSerializer(serializers.Serializer):
    service_id = serializers.IntegerField(min_value=1)
    starts_at = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(min_value=30, max_value=480)
    meeting_address = serializers.CharField(max_length=255)
    longitude = serializers.DecimalField(max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(max_digits=10, decimal_places=7)
    contact_name = serializers.CharField(max_length=30)
    contact_phone = serializers.RegexField(r"^1\d{10}$")
    note = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, attrs):
        try:
            service = ProviderService.objects.select_related(
                "provider__user", "category"
            ).get(
                id=attrs["service_id"],
                is_active=True,
                provider__status=ProviderProfile.Status.APPROVED,
            )
        except ProviderService.DoesNotExist as exc:
            raise serializers.ValidationError({"service_id": "服务不存在或不可预约。"}) from exc
        duration, ends_at = validate_booking(
            service, attrs["starts_at"], attrs["duration_minutes"]
        )
        ensure_booking_within_schedule(service.provider, attrs["starts_at"], ends_at)
        provider = service.provider
        if provider.source_longitude is None or provider.source_latitude is None:
            raise serializers.ValidationError({"service_id": "达人尚未配置服务中心，暂时无法预约。"})
        route = tencent_map.driving_route(
            provider.source_longitude,
            provider.source_latitude,
            attrs["longitude"],
            attrs["latitude"],
        )
        if route.distance_km > provider.max_service_radius_km:
            raise serializers.ValidationError(
                {"meeting_address": f"该地点距达人约{route.distance_km}公里，超出{provider.max_service_radius_km}公里服务范围。"}
            )
        attrs["service"] = service
        attrs["duration_minutes"] = duration
        attrs["ends_at"] = ends_at
        attrs["route"] = route
        attrs["quote"] = build_quote(service, duration, route.distance_km)
        return attrs


class ProviderOrderSerializer(serializers.ModelSerializer):
    provider_public_id = serializers.UUIDField(source="provider.user.public_id")
    provider_name = serializers.CharField(source="provider_name_snapshot")
    provider_avatar_url = serializers.SerializerMethodField()
    service_name = serializers.CharField(source="service_name_snapshot")
    status_label = serializers.CharField(source="get_status_display")
    contact_phone_masked = serializers.SerializerMethodField()

    class Meta:
        model = ProviderOrder
        fields = (
            "public_id", "order_no", "status", "status_label", "provider_public_id",
            "provider_name", "provider_avatar_url", "service_name", "billing_type_snapshot",
            "unit_price_amount", "starts_at", "ends_at", "duration_minutes", "meeting_address",
            "contact_name", "contact_phone_masked", "note", "service_fee_amount",
            "transport_fee_amount", "other_fee_amount", "discount_amount", "payable_amount",
            "pricing_snapshot", "payment_expires_at", "paid_at", "created_at",
        )

    def get_provider_avatar_url(self, obj):
        return build_media_url(obj.provider.user.avatar_object_key)

    def get_contact_phone_masked(self, obj):
        phone = obj.contact_phone
        return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone


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
