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
        if validated_data.get("is_default"):
            UserAddress.objects.filter(user=user, is_default=True).update(is_default=False)
        return UserAddress.objects.create(user=user, **validated_data)
