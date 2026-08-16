from django.urls import path

from .views import ActivityDetailView, ActivityListView

urlpatterns = [
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activities/<int:pk>/", ActivityDetailView.as_view(), name="activity-detail"),
]
