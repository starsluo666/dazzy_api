from django.contrib import admin

from .models import ScheduledTask


@admin.register(ScheduledTask)
class ScheduledTaskAdmin(admin.ModelAdmin):
    list_display = (
        "public_id",
        "task_type",
        "business_key",
        "status",
        "scheduled_at",
        "attempt_count",
        "updated_at",
    )
    list_filter = ("task_type", "status")
    search_fields = ("business_key", "dedupe_key")
    readonly_fields = (
        "public_id",
        "dedupe_key",
        "attempt_count",
        "created_at",
        "updated_at",
    )

