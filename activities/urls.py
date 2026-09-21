from django.urls import path

from .views import (
    ActivityAfterSalesView,
    ActivityCategoryListView,
    ActivityCopySourceView,
    ActivityDetailView,
    ActivityListView,
    ActivityOrganizerCancelView,
    ActivityParticipationPaymentView,
    ActivityParticipationPaymentAuthorizationView,
    ActivityParticipationPaymentSessionView,
    ActivityParticipationPaymentStatusView,
    ActivityParticipationView,
    ActivityPublishOrderView,
    ActivityPublishPaymentView,
    ActivityPublishPaymentAuthorizationView,
    ActivityPublishPaymentSessionView,
    ActivityPublishPaymentStatusView,
    ActivityPublishRuleView,
    ActivityReportCreateView,
    MyActivityListView,
)

urlpatterns = [
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activity-categories/", ActivityCategoryListView.as_view(), name="activity-category-list"),
    path("activity-publish-rules/", ActivityPublishRuleView.as_view(), name="activity-publish-rules"),
    path("activities/mine/", MyActivityListView.as_view(), name="my-activity-list"),
    path("activities/<int:pk>/", ActivityDetailView.as_view(), name="activity-detail"),
    path("activities/<int:pk>/copy-source/", ActivityCopySourceView.as_view(), name="activity-copy-source"),
    path("activities/<int:pk>/reports/", ActivityReportCreateView.as_view(), name="activity-report-create"),
    path(
        "activities/<int:pk>/participation/",
        ActivityParticipationView.as_view(),
        name="activity-participation",
    ),
    path(
        "activities/<int:pk>/participation/simulate-payment/",
        ActivityParticipationPaymentView.as_view(),
        name="activity-participation-payment",
    ),
    path(
        "activities/<int:pk>/participation/payment-authorization/",
        ActivityParticipationPaymentAuthorizationView.as_view(),
        name="activity-participation-payment-authorization",
    ),
    path(
        "activities/<int:pk>/participation/payment-session/",
        ActivityParticipationPaymentSessionView.as_view(),
        name="activity-participation-payment-session",
    ),
    path(
        "activities/<int:pk>/participation/payment-status/",
        ActivityParticipationPaymentStatusView.as_view(),
        name="activity-participation-payment-status",
    ),
    path(
        "activities/<int:pk>/after-sales/",
        ActivityAfterSalesView.as_view(),
        name="activity-after-sales",
    ),
    path(
        "activities/<int:pk>/cancel/",
        ActivityOrganizerCancelView.as_view(),
        name="activity-organizer-cancel",
    ),
    path("activities/<int:pk>/publish-order/", ActivityPublishOrderView.as_view(), name="activity-publish-order"),
    path("activities/<int:pk>/publish-order/simulate-payment/", ActivityPublishPaymentView.as_view(), name="activity-publish-payment"),
    path(
        "activities/<int:pk>/publish-order/payment-authorization/",
        ActivityPublishPaymentAuthorizationView.as_view(),
        name="activity-publish-payment-authorization",
    ),
    path(
        "activities/<int:pk>/publish-order/payment-session/",
        ActivityPublishPaymentSessionView.as_view(),
        name="activity-publish-payment-session",
    ),
    path(
        "activities/<int:pk>/publish-order/payment-status/",
        ActivityPublishPaymentStatusView.as_view(),
        name="activity-publish-payment-status",
    ),
]
