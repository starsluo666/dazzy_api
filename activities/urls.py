from django.urls import path

from .views import ActivityListView

urlpatterns = [path("activities/", ActivityListView.as_view(), name="activity-list")]
