from django.urls import path

from .views import (
    CurrentProviderApplicationSubmitView,
    CurrentProviderApplicationView,
    CurrentProviderServiceDetailView,
    CurrentProviderServiceListCreateView,
    ProviderAvailabilityView,
    ProviderDetailView,
    ProviderListView,
    ProviderReviewListView,
    ServiceCategoryListView,
    CurrentProviderScheduleDayView,
    CurrentProviderSchedulePeriodView,
    CurrentProviderScheduleView,
    CurrentProviderOnlineLocationView,
    CurrentProviderOnlineStartView,
    CurrentProviderOnlineStopView,
    CurrentProviderWorkbenchView,
)

urlpatterns = [
    path("providers/", ProviderListView.as_view(), name="provider-list"),
    path("service-categories/", ServiceCategoryListView.as_view(), name="service-category-list"),
    path(
        "providers/me/application/",
        CurrentProviderApplicationView.as_view(),
        name="provider-application",
    ),
    path(
        "providers/me/application/submit/",
        CurrentProviderApplicationSubmitView.as_view(),
        name="provider-application-submit",
    ),
    path(
        "providers/me/services/",
        CurrentProviderServiceListCreateView.as_view(),
        name="provider-service-list",
    ),
    path(
        "providers/me/services/<int:service_id>/",
        CurrentProviderServiceDetailView.as_view(),
        name="provider-service-detail",
    ),
    path(
        "providers/me/workbench/", CurrentProviderWorkbenchView.as_view(), name="provider-workbench"
    ),
    path(
        "providers/me/online/start/",
        CurrentProviderOnlineStartView.as_view(),
        name="provider-online-start",
    ),
    path(
        "providers/me/online/location/",
        CurrentProviderOnlineLocationView.as_view(),
        name="provider-online-location",
    ),
    path(
        "providers/me/online/stop/",
        CurrentProviderOnlineStopView.as_view(),
        name="provider-online-stop",
    ),
    path("providers/me/schedule/", CurrentProviderScheduleView.as_view(), name="provider-schedule"),
    path(
        "providers/me/schedule/periods/<str:period_id>/",
        CurrentProviderSchedulePeriodView.as_view(),
        name="provider-schedule-period",
    ),
    path(
        "providers/me/schedule/days/<str:day>/",
        CurrentProviderScheduleDayView.as_view(),
        name="provider-schedule-day",
    ),
    path(
        "providers/<uuid:public_id>/",
        ProviderDetailView.as_view(),
        name="provider-detail",
    ),
    path(
        "providers/<uuid:public_id>/availability/",
        ProviderAvailabilityView.as_view(),
        name="provider-availability",
    ),
    path(
        "providers/<uuid:public_id>/reviews/",
        ProviderReviewListView.as_view(),
        name="provider-reviews",
    ),
]
