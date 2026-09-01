from rest_framework import serializers

from .models import ScheduledTask


class ScheduledTaskQuerySerializer(serializers.Serializer):
    task_type = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ScheduledTask.Type.choices,
    )
    status = serializers.ChoiceField(
        required=False,
        allow_blank=True,
        choices=ScheduledTask.Status.choices,
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=100)
    overdue = serializers.BooleanField(required=False, default=False)
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=100, default=20)


class ScheduledTaskSerializer(serializers.ModelSerializer):
    task_type_label = serializers.CharField(source="get_task_type_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = ScheduledTask
        fields = (
            "public_id",
            "task_type",
            "task_type_label",
            "business_type",
            "business_key",
            "status",
            "status_label",
            "scheduled_at",
            "available_at",
            "attempt_count",
            "max_attempts",
            "started_at",
            "finished_at",
            "last_error",
            "payload",
            "result",
            "created_at",
            "updated_at",
        )
