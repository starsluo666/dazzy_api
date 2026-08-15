from django.contrib import admin

from .models import Activity, ActivityCategory


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
