from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


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
