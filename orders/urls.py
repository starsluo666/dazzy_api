from django.urls import path

from .views import (
    CurrentProviderOrderAcceptView,
    CurrentProviderOrderArrivalEvidenceView,
    CurrentProviderOrderCompleteView,
    CurrentProviderOrderDepartView,
    CurrentProviderOrderDetailView,
    CurrentProviderOrderListView,
    CurrentProviderOrderRejectView,
    CurrentProviderOrderStartView,
    CurrentUserProviderReviewListView,
    HuifuPaymentNotificationView,
    PaymentCapabilitiesView,
    ProviderOrderAfterSalesView,
    ProviderOrderCancelView,
    ProviderOrderConfirmCompletionView,
    ProviderOrderDetailView,
    ProviderOrderListCreateView,
    ProviderOrderPaymentAuthorizationView,
    ProviderOrderPaymentSessionView,
    ProviderOrderPaymentStatusView,
    ProviderOrderPreviewView,
    ProviderOrderReviewView,
    ProviderOrderSimulatePaymentView,
    WechatOfficialOAuthCallbackView,
)

urlpatterns = [
    path("payments/capabilities/", PaymentCapabilitiesView.as_view()),
    path("payments/huifu/notify/", HuifuPaymentNotificationView.as_view()),
    path(
        "payments/wechat/oauth/callback/",
        WechatOfficialOAuthCallbackView.as_view(),
    ),
    path("providers/me/orders/", CurrentProviderOrderListView.as_view()),
    path("providers/me/orders/<str:order_no>/", CurrentProviderOrderDetailView.as_view()),
    path(
        "providers/me/orders/<str:order_no>/accept/",
        CurrentProviderOrderAcceptView.as_view(),
    ),
    path(
        "providers/me/orders/<str:order_no>/reject/",
        CurrentProviderOrderRejectView.as_view(),
    ),
    path(
        "providers/me/orders/<str:order_no>/depart/",
        CurrentProviderOrderDepartView.as_view(),
    ),
    path(
        "providers/me/orders/<str:order_no>/arrival-evidence/",
        CurrentProviderOrderArrivalEvidenceView.as_view(),
    ),
    path(
        "providers/me/orders/<str:order_no>/start/",
        CurrentProviderOrderStartView.as_view(),
    ),
    path(
        "providers/me/orders/<str:order_no>/complete/",
        CurrentProviderOrderCompleteView.as_view(),
    ),
    path("provider-orders/preview/", ProviderOrderPreviewView.as_view()),
    path("provider-orders/", ProviderOrderListCreateView.as_view()),
    path("provider-orders/<str:order_no>/", ProviderOrderDetailView.as_view()),
    path(
        "provider-orders/<str:order_no>/after-sales/",
        ProviderOrderAfterSalesView.as_view(),
    ),
    path("provider-orders/<str:order_no>/cancel/", ProviderOrderCancelView.as_view()),
    path(
        "provider-orders/<str:order_no>/confirm-completion/",
        ProviderOrderConfirmCompletionView.as_view(),
    ),
    path("provider-orders/<str:order_no>/review/", ProviderOrderReviewView.as_view()),
    path(
        "provider-orders/<str:order_no>/payment-authorization/",
        ProviderOrderPaymentAuthorizationView.as_view(),
    ),
    path(
        "provider-orders/<str:order_no>/payment-session/",
        ProviderOrderPaymentSessionView.as_view(),
    ),
    path(
        "provider-orders/<str:order_no>/payment-status/",
        ProviderOrderPaymentStatusView.as_view(),
    ),
    path("users/me/provider-reviews/", CurrentUserProviderReviewListView.as_view()),
    path("provider-orders/<str:order_no>/simulate-payment/", ProviderOrderSimulatePaymentView.as_view()),
]
