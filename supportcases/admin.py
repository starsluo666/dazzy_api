from django.contrib import admin

from .models import SupportCase, SupportCaseRecord


@admin.register(SupportCase)
class SupportCaseAdmin(admin.ModelAdmin):
    list_display = (
        "case_no", "case_type", "target_type", "reason", "status", "reporter",
        "assignee", "created_at",
    )
    list_filter = ("case_type", "target_type", "reason", "status")
    search_fields = ("case_no", "reporter__phone", "reporter__nickname", "description")
    readonly_fields = tuple(field.name for field in SupportCase._meta.fields)


@admin.register(SupportCaseRecord)
class SupportCaseRecordAdmin(admin.ModelAdmin):
    list_display = ("case", "record_type", "actor", "from_status", "to_status", "created_at")
    list_filter = ("record_type",)
    search_fields = ("case__case_no", "content")
    readonly_fields = tuple(field.name for field in SupportCaseRecord._meta.fields)
