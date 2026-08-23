from django.contrib import admin

from .models import (
    AdminAuditLog,
    AdminRole,
    Organization,
    OrganizationMember,
    ProviderCreditAdjustment,
    ProviderOrderAfterSalesCase,
    ProviderOrderSupportNote,
    UserRiskFlag,
)


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organization_type", "parent", "status")
    list_filter = ("organization_type", "status")
    search_fields = ("name", "code")


@admin.register(AdminRole)
class AdminRoleAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "organization", "data_scope", "is_system")
    list_filter = ("data_scope", "is_system")


@admin.register(OrganizationMember)
class OrganizationMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "organization", "role", "is_active")
    list_filter = ("organization", "role", "is_active")
    search_fields = ("user__nickname", "user__phone")


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "organization", "action", "target_type", "target_id")
    list_filter = ("organization", "action", "target_type")
    search_fields = ("actor__nickname", "actor__phone", "target_id")
    readonly_fields = tuple(field.name for field in AdminAuditLog._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderOrderSupportNote)
class ProviderOrderSupportNoteAdmin(admin.ModelAdmin):
    list_display = ("created_at", "order", "author", "organization")
    list_filter = ("organization",)
    search_fields = ("order__order_no", "author__nickname", "content")
    readonly_fields = tuple(field.name for field in ProviderOrderSupportNote._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderOrderAfterSalesCase)
class ProviderOrderAfterSalesCaseAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "case_no", "order", "case_type", "status",
        "requested_amount", "approved_amount", "reviewed_by",
    )
    list_filter = ("case_type", "status", "organization")
    search_fields = ("case_no", "order__order_no", "reason", "result_note")
    readonly_fields = tuple(field.name for field in ProviderOrderAfterSalesCase._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UserRiskFlag)
class UserRiskFlagAdmin(admin.ModelAdmin):
    list_display = ("user", "level", "is_active", "marked_by", "organization", "updated_at")
    list_filter = ("level", "is_active", "organization")
    search_fields = ("user__nickname", "user__phone", "reason")
    readonly_fields = tuple(field.name for field in UserRiskFlag._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProviderCreditAdjustment)
class ProviderCreditAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "provider", "delta", "before_score", "after_score", "operator")
    list_filter = ("organization",)
    search_fields = ("provider__user__nickname", "provider__user__phone", "reason")
    readonly_fields = tuple(field.name for field in ProviderCreditAdjustment._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
