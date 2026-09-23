from django.urls import path

from .views import (
    ActivityCoverUploadView,
    AvatarUploadView,
    HomeCardAssetView,
    OrderEvidenceUploadView,
    ReviewImageUploadView,
    SupportAttachmentUploadView,
    ProviderLifestylePhotoUploadView,
    ProviderVideoUploadView,
    ProviderIdentityPhotoUploadView,
)

urlpatterns = [
    path("media/provider-videos/", ProviderVideoUploadView.as_view(), name="provider-video-upload"),
    path("content/home-cards/", HomeCardAssetView.as_view(), name="home-card-assets"),
    path("media/activity-covers/", ActivityCoverUploadView.as_view(), name="activity-cover-upload"),
    path(
        "media/provider-lifestyle-photos/",
        ProviderLifestylePhotoUploadView.as_view(),
        name="provider-lifestyle-photo-upload",
    ),
    path(
        "media/provider-identities/",
        ProviderIdentityPhotoUploadView.as_view(),
        name="provider-identity-photo-upload",
    ),
    path("media/avatars/", AvatarUploadView.as_view(), name="avatar-upload"),
    path("media/order-evidence/", OrderEvidenceUploadView.as_view(), name="order-evidence-upload"),
    path("media/review-images/", ReviewImageUploadView.as_view(), name="review-image-upload"),
    path(
        "media/support-attachments/",
        SupportAttachmentUploadView.as_view(),
        name="support-attachment-upload",
    ),
]
