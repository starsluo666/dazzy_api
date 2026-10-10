from django.urls import path

from .views import GrowthCampaignView, MyInvitationSummaryView
from .provider_invite_views import PublicProviderInviteView, InvitedProviderRegisterView, MyProviderInvitesView

urlpatterns = [
    path("growth/provider-invites/register/", InvitedProviderRegisterView.as_view()),
    path("growth/provider-invites/me/", MyProviderInvitesView.as_view()),
    path("growth/provider-invites/<str:code>/", PublicProviderInviteView.as_view()),
    path("growth/campaign/", GrowthCampaignView.as_view(), name="growth-campaign"),
    path("growth/invitations/me/", MyInvitationSummaryView.as_view(), name="my-invitations"),
]
