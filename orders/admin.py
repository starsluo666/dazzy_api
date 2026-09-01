from django.contrib import admin

from .models import ProviderOrder


@admin.register(ProviderOrder)
class ProviderOrderAdmin(admin.ModelAdmin):
    list_display = ("order_no", "customer", "provider", "status", "payable_amount", "starts_at")
    list_filter = ("status", "billing_type_snapshot")
    search_fields = ("order_no", "customer__phone", "provider_name_snapshot")
    readonly_fields = (
        "public_id", "departed_at", "arrival_photo_uploaded_at", "service_started_at",
        "completion_submitted_at", "confirmation_expires_at", "customer_confirmed_at",
        "auto_confirmed_at", "created_at", "updated_at",
    )
