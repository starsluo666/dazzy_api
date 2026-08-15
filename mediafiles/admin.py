from django.contrib import admin

from .models import MediaAsset


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "scope", "category", "status", "size_bytes", "created_at")
    list_filter = ("scope", "category", "status")
    search_fields = ("id", "object_key", "owner__phone")
    readonly_fields = ("id", "created_at", "updated_at")
