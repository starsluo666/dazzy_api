from django.urls import path

from .views import ActivityDetailView, ActivityListView, ActivityParticipationView, MyActivityListView

urlpatterns = [
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activities/mine/", MyActivityListView.as_view(), name="my-activity-list"),
    path("activities/<int:pk>/", ActivityDetailView.as_view(), name="activity-detail"),
    path(
        "activities/<int:pk>/participation/",
        ActivityParticipationView.as_view(),
        name="activity-participation",
    ),
]
