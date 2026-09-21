from django.contrib import admin

from .models import (
    ProviderCategoryGrant,
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderProfile,
    ProviderProfileRevision,
    ProviderService,
    ProviderServiceRevision,
    ProviderWeeklyAvailability,
    ServiceCategory,
)


@admin.register(ServiceCategory)
class ServiceCategoryAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "hourly_min_price_amount",
        "hourly_max_price_amount",
        "per_session_min_price_amount",
        "per_session_max_price_amount",
        "sort_order",
        "is_active",
    )
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
        "display_name",
        "status",
        "onboarding_status",
        "service_city_name",
        "max_service_radius_km",
        "rating",
        "credit_score",
        "submitted_at",
    )
    list_filter = ("status", "onboarding_status", "identity_status", "service_city_code")
    search_fields = (
        "user__phone",
        "user__nickname",
        "display_name",
        "application_real_name",
        "service_city_name",
    )
    readonly_fields = (
        "submitted_at",
        "agreement_accepted_at",
        "onboarding_submitted_at",
        "onboarding_reviewed_at",
        "created_at",
        "updated_at",
    )
    inlines = (ProviderServiceInline, ProviderWeeklyAvailabilityInline)


@admin.register(ProviderService)
class ProviderServiceAdmin(admin.ModelAdmin):
    list_display = ("provider", "category", "billing_type", "price_amount", "is_active")
    list_filter = ("category", "billing_type", "is_active")
    search_fields = ("provider__user__phone", "provider__user__nickname")


@admin.register(ProviderCategoryGrant)
class ProviderCategoryGrantAdmin(admin.ModelAdmin):
    list_display = ("provider", "category", "is_active", "granted_by", "granted_at")
    list_filter = ("is_active", "category")
    search_fields = (
        "provider__user__phone",
        "provider__display_name",
        "provider__application_real_name",
    )
    readonly_fields = ("granted_at", "revoked_at")


@admin.register(ProviderProfileRevision)
class ProviderProfileRevisionAdmin(admin.ModelAdmin):
    list_display = ("provider", "display_name", "status", "submitted_at", "reviewed_by")
    list_filter = ("status", "service_city_code")
    search_fields = ("provider__user__phone", "provider__display_name", "display_name")
    readonly_fields = ("submitted_at", "reviewed_at", "created_at", "updated_at")


@admin.register(ProviderServiceRevision)
class ProviderServiceRevisionAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "category",
        "action",
        "billing_type",
        "price_amount",
        "status",
        "submitted_at",
    )
    list_filter = ("status", "action", "billing_type", "category")
    search_fields = ("provider__user__phone", "provider__display_name")
    readonly_fields = ("submitted_at", "reviewed_at", "created_at", "updated_at")


@admin.register(ProviderWeeklyAvailability)
class ProviderWeeklyAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("provider", "weekday", "starts_at", "ends_at", "is_active")
    list_filter = ("weekday", "is_active")
    search_fields = ("provider__user__phone", "provider__user__nickname")


admin.site.register(ProviderDateAvailability)
admin.site.register(ProviderDateClosure)
