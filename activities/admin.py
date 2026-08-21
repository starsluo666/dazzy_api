from django.contrib import admin

from .models import Activity, ActivityCategory, ActivityParticipation, ActivityPublishOrder


@admin.register(ActivityCategory)
class ActivityCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "slug")


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "organizer",
        "category",
        "status",
        "starts_at",
        "capacity",
        "aa_principal_amount",
    )
    list_filter = ("status", "category", "map_source")
    search_fields = ("title", "organizer__phone", "organizer__nickname", "meeting_place_name")
    readonly_fields = ("created_at", "updated_at", "published_at")
    date_hierarchy = "starts_at"


@admin.register(ActivityParticipation)
class ActivityParticipationAdmin(admin.ModelAdmin):
    list_display = ("activity", "user", "status", "joined_at", "cancelled_at")
    list_filter = ("status",)
    search_fields = ("activity__title", "user__phone", "user__nickname")
    readonly_fields = ("joined_at", "created_at", "updated_at")


@admin.register(ActivityPublishOrder)
class ActivityPublishOrderAdmin(admin.ModelAdmin):
    list_display = ("order_no", "activity", "payer", "payable_amount", "status", "paid_at")
    list_filter = ("status",)
    search_fields = ("order_no", "activity__title", "payer__phone")
    readonly_fields = ("created_at", "updated_at", "paid_at")
