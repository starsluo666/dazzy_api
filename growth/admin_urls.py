from django.urls import path

from .admin_views import AdminGrowthConfigView, AdminInvitationListView

urlpatterns = [
    path("growth/config/", AdminGrowthConfigView.as_view(), name="admin-growth-config"),
    path("growth/invitations/", AdminInvitationListView.as_view(), name="admin-invitations"),
]
