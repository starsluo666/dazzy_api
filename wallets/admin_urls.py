from django.urls import path

from .admin_views import (
    AdminRechargeCampaignView,
    AdminRechargeOrderListView,
    AdminWalletListView,
)


urlpatterns = [
    path("wallets/", AdminWalletListView.as_view()),
    path("recharge-orders/", AdminRechargeOrderListView.as_view()),
    path("recharge-campaign/", AdminRechargeCampaignView.as_view()),
]
