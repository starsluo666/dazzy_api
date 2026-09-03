from django.urls import path

from .admin_views import (
    AdminSupportCaseActionView,
    AdminSupportCaseDetailView,
    AdminSupportCaseListView,
    AdminSupportCaseReplyView,
)

urlpatterns = [
    path("support-cases/", AdminSupportCaseListView.as_view(), name="admin-support-cases"),
    path(
        "support-cases/<str:case_no>/",
        AdminSupportCaseDetailView.as_view(),
        name="admin-support-case-detail",
    ),
    path(
        "support-cases/<str:case_no>/action/",
        AdminSupportCaseActionView.as_view(),
        name="admin-support-case-action",
    ),
    path(
        "support-cases/<str:case_no>/reply/",
        AdminSupportCaseReplyView.as_view(),
        name="admin-support-case-reply",
    ),
]
