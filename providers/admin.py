from django.contrib import admin

from .models import (
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name", "slug")


class ProviderServiceInline(admin.TabularInline):
    model = ProviderService
    extra = 0


class ProviderWeeklyAvailabilityInline(admin.TabularInline):
    model = ProviderWeeklyAvailability
    extra = 0


@admin.register(ProviderProfile)
class ProviderProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "status",
        "service_city_name",
        "max_service_radius_km",
        "rating",
        "credit_score",
        "submitted_at",
    )
    list_filter = ("status", "service_city_code")
    search_fields = ("user__phone", "user__nickname", "service_city_name")
    readonly_fields = ("submitted_at", "agreement_accepted_at", "created_at", "updated_at")
    inlines = (ProviderServiceInline, ProviderWeeklyAvailabilityInline)


@admin.register(ProviderService)
class ProviderServiceAdmin(admin.ModelAdmin):
    list_display = ("provider", "category", "billing_type", "price_amount", "is_active")
    list_filter = ("category", "billing_type", "is_active")
    search_fields = ("provider__user__phone", "provider__user__nickname")


@admin.register(ProviderWeeklyAvailability)
class ProviderWeeklyAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("provider", "weekday", "starts_at", "ends_at", "is_active")
    list_filter = ("weekday", "is_active")
    search_fields = ("provider__user__phone", "provider__user__nickname")


admin.site.register(ProviderDateAvailability)
admin.site.register(ProviderDateClosure)
