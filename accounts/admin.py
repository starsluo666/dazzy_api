from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User, WechatOfficialAccountIdentity


@admin.register(User)
class DazzyUserAdmin(UserAdmin):
    ordering = ("-date_joined",)
    list_display = ("phone", "nickname", "account_status", "is_active")
    search_fields = ("phone", "nickname", "public_id")
    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        (
            "用户资料",
            {"fields": ("public_id", "nickname", "gender", "birth_date", "avatar_object_key")},
        ),
        ("账号状态", {"fields": ("account_status", "is_active")}),
        ("权限", {"fields": ("is_staff", "is_superuser", "groups", "user_permissions")}),
        ("时间", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("public_id", "last_login", "date_joined")
    add_fieldsets = ((None, {"fields": ("phone", "password1", "password2", "is_staff")}),)


@admin.register(WechatOfficialAccountIdentity)
class WechatOfficialAccountIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "app_id", "masked_openid", "authorized_at", "updated_at")
    search_fields = ("user__phone", "user__nickname", "app_id", "openid")
    fields = (
        "user",
        "app_id",
        "masked_openid",
        "masked_unionid",
        "authorized_at",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    @admin.display(description="OpenID")
    def masked_openid(self, obj):
        if len(obj.openid) <= 10:
            return "***"
        return f"{obj.openid[:5]}***{obj.openid[-5:]}"

    @admin.display(description="UnionID")
    def masked_unionid(self, obj):
        if not obj.unionid:
            return "-"
        if len(obj.unionid) <= 10:
            return "***"
        return f"{obj.unionid[:5]}***{obj.unionid[-5:]}"
