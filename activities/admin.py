from django.contrib import admin

from .models import (
    Activity,
    ActivityCategory,
    ActivityHuifuNotification,
    ActivityHuifuPaymentOrder,
    ActivityHuifuRefundOrder,
    ActivityParticipation,
    ActivityPublishOrder,
)


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


class ReadOnlyPaymentAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ActivityHuifuPaymentOrder)
class ActivityHuifuPaymentOrderAdmin(ReadOnlyPaymentAdmin):
    list_display = (
        "req_seq_id", "preorder_status", "trade_type", "gateway_last_query_status",
        "gateway_close_status", "updated_at",
    )
    list_filter = (
        "preorder_status", "trade_type", "gateway_last_query_status",
        "gateway_close_status",
    )
    search_fields = ("req_seq_id", "gateway_trade_no", "gateway_merchant_id")


@admin.register(ActivityHuifuRefundOrder)
class ActivityHuifuRefundOrderAdmin(ReadOnlyPaymentAdmin):
    list_display = (
        "req_seq_id", "gateway_status", "gateway_last_query_status", "updated_at",
    )
    list_filter = ("gateway_status", "gateway_last_query_status")
    search_fields = ("req_seq_id", "gateway_refund_no", "gateway_merchant_id")


@admin.register(ActivityHuifuNotification)
class ActivityHuifuNotificationAdmin(ReadOnlyPaymentAdmin):
    list_display = (
        "req_seq_id", "trans_type", "trans_stat", "status", "received_at",
    )
    list_filter = ("status", "trans_type", "trans_stat")
    search_fields = ("req_seq_id", "hf_seq_id", "event_key")
