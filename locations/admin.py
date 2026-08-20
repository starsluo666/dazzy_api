from django.contrib import admin

from .models import UserAddress


@admin.register(UserAddress)
class UserAddressAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "city_name", "is_default", "updated_at")
    list_filter = ("city_name", "is_default")
    search_fields = ("name", "address", "user__phone")
