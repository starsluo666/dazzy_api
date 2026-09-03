from django.urls import path

from .views import (
    SupportCaseDetailView,
    SupportCaseListCreateView,
    SupportCaseReplyView,
    SupportCaseReviewRequestView,
)

urlpatterns = [
    path("support/cases/", SupportCaseListCreateView.as_view(), name="support-case-list"),
    path(
        "support/cases/<str:case_no>/",
        SupportCaseDetailView.as_view(),
        name="support-case-detail",
    ),
    path(
        "support/cases/<str:case_no>/reply/",
        SupportCaseReplyView.as_view(),
        name="support-case-reply",
    ),
    path(
        "support/cases/<str:case_no>/review/",
        SupportCaseReviewRequestView.as_view(),
        name="support-case-review",
    ),
]
