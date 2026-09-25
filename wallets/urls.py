from django.urls import path

from .views import (
    CurrentWalletView,
    RechargeCampaignView,
    RechargeOrderAuthorizationView,
    RechargeOrderListCreateView,
    RechargeOrderPaymentSessionView,
    RechargeOrderPaymentStatusView,
)


urlpatterns = [
    path("users/me/wallet/", CurrentWalletView.as_view()),
    path("wallet/recharge-campaign/", RechargeCampaignView.as_view()),
    path("wallet/recharge-orders/", RechargeOrderListCreateView.as_view()),
    path(
        "wallet/recharge-orders/<str:order_no>/payment-authorization/",
        RechargeOrderAuthorizationView.as_view(),
    ),
    path(
        "wallet/recharge-orders/<str:order_no>/payment-session/",
        RechargeOrderPaymentSessionView.as_view(),
    ),
    path(
        "wallet/recharge-orders/<str:order_no>/payment-status/",
        RechargeOrderPaymentStatusView.as_view(),
    ),
]
