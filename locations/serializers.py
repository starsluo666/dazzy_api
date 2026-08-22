from django.db import transaction
from rest_framework import serializers

from .models import UserAddress


class UserAddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserAddress
        fields = (
            "id", "name", "address", "city_name", "longitude", "latitude",
            "is_default", "created_at", "updated_at",
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
