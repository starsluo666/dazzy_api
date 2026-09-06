from rest_framework import serializers

from activities.serializers import ActivityListItemSerializer
from providers.serializers import ProviderListItemSerializer


class HomeQuerySerializer(serializers.Serializer):
    city_code = serializers.CharField(required=False, max_length=20)
    longitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(required=False, max_digits=10, decimal_places=7)

    def validate(self, attrs):
        if ("longitude" in attrs) != ("latitude" in attrs):
            raise serializers.ValidationError("longitude 和 latitude 必须同时提供。")
        return attrs


class HomeProviderSerializer(ProviderListItemSerializer):
    availability_status = serializers.SerializerMethodField()
    earliest_available_at = serializers.SerializerMethodField()
    is_favorited = serializers.SerializerMethodField()

    class Meta(ProviderListItemSerializer.Meta):
        fields = ProviderListItemSerializer.Meta.fields + (
            "availability_status",
            "earliest_available_at",
            "is_favorited",
        )

    def get_availability_status(self, obj) -> str:
        return (
            "available"
            if getattr(obj, "is_currently_online", False)
            and self.context["earliest_by_provider"].get(obj.pk)
            else "unavailable"
        )

    def get_earliest_available_at(self, obj):
        if not getattr(obj, "is_currently_online", False):
            return None
        slot = self.context["earliest_by_provider"].get(obj.pk)
        return slot["starts_at"] if slot else None

    def get_is_favorited(self, obj) -> bool:
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.favorited_by.filter(user=request.user).exists())


class HomeActivitySerializer(ActivityListItemSerializer):
    pass
