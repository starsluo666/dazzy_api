from rest_framework import serializers

from .models import UserNotification


class NotificationListQuerySerializer(serializers.Serializer):
    category = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=UserNotification.Category.choices,
    )
    is_read = serializers.BooleanField(required=False)
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(
        required=False, default=20, min_value=1, max_value=50
    )


class NotificationReadAllSerializer(serializers.Serializer):
    category = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=UserNotification.Category.choices,
    )


class UserNotificationSerializer(serializers.ModelSerializer):
    category_label = serializers.CharField(source="get_category_display")
    event_type_label = serializers.CharField(source="get_event_type_display")
    is_read = serializers.SerializerMethodField()

    class Meta:
        model = UserNotification
        fields = (
            "public_id",
            "category",
            "category_label",
            "event_type",
            "event_type_label",
            "title",
            "content",
            "target_type",
            "target_id",
            "target_title",
            "action_text",
            "action_url",
            "is_read",
            "read_at",
            "created_at",
        )

    def get_is_read(self, obj):
        return obj.read_at is not None
