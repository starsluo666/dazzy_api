from django.urls import path

from .views import ActivityCoverUploadView, AvatarUploadView, HomeCardAssetView

urlpatterns = [
    path("content/home-cards/", HomeCardAssetView.as_view(), name="home-card-assets"),
    path("media/activity-covers/", ActivityCoverUploadView.as_view(), name="activity-cover-upload"),
    path("media/avatars/", AvatarUploadView.as_view(), name="avatar-upload"),
]
