from django.contrib import admin

from .models import UserNotification


@admin.register(UserNotification)
class UserNotificationAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "recipient",
        "category",
        "event_type",
        "read_at",
        "created_at",
    )
    list_filter = ("category", "event_type", "read_at")
    search_fields = ("title", "content", "recipient__phone", "target_id")
    readonly_fields = ("public_id", "dedupe_key", "created_at")
