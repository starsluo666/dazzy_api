from django.db import transaction
from rest_framework import serializers

from .models import UserAddress


class CoordinatesQuerySerializer(serializers.Serializer):
    longitude = serializers.DecimalField(max_digits=10, decimal_places=7)
    latitude = serializers.DecimalField(max_digits=10, decimal_places=7)

    def validate_longitude(self, value):
        if not -180 <= value <= 180:
            raise serializers.ValidationError("经度必须在 -180 到 180 之间。")
        return value

    def validate_latitude(self, value):
        if not -90 <= value <= 90:
            raise serializers.ValidationError("纬度必须在 -90 到 90 之间。")
        return value


class UserAddressSerializer(serializers.ModelSerializer):
    contact_name = serializers.CharField(max_length=30)
    contact_gender = serializers.ChoiceField(choices=UserAddress.ContactGender.choices)
    contact_gender_label = serializers.CharField(
        source="get_contact_gender_display", read_only=True
    )
    contact_phone = serializers.RegexField(r"^1\d{10}$")

    class Meta:
        model = UserAddress
        fields = (
            "id", "name", "address", "city_name", "contact_name",
            "contact_gender", "contact_gender_label", "contact_phone",
            "longitude", "latitude", "is_default", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    @transaction.atomic
    def create(self, validated_data):
        user = self.context["request"].user
        if not UserAddress.objects.filter(user=user).exists():
            validated_data["is_default"] = True
        elif validated_data.get("is_default"):
            UserAddress.objects.filter(user=user, is_default=True).update(is_default=False)
        return UserAddress.objects.create(user=user, **validated_data)

    @transaction.atomic
    def update(self, instance, validated_data):
        if instance.is_default and validated_data.get("is_default") is False:
            validated_data["is_default"] = True
        if validated_data.get("is_default"):
            UserAddress.objects.filter(user=instance.user, is_default=True).exclude(
                pk=instance.pk
            ).update(is_default=False)
        return super().update(instance, validated_data)
