from django.urls import path

from .views import GrowthCampaignView, MyInvitationSummaryView

urlpatterns = [
    path("growth/campaign/", GrowthCampaignView.as_view(), name="growth-campaign"),
    path("growth/invitations/me/", MyInvitationSummaryView.as_view(), name="my-invitations"),
]
