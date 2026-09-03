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
    ProviderOrderCancelView,
    ProviderOrderConfirmCompletionView,
    ProviderOrderDetailView,
    ProviderOrderListCreateView,
    ProviderOrderPreviewView,
    ProviderOrderReviewView,
    ProviderOrderSimulatePaymentView,
)

urlpatterns = [
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
    path("provider-orders/<str:order_no>/cancel/", ProviderOrderCancelView.as_view()),
    path(
        "provider-orders/<str:order_no>/confirm-completion/",
        ProviderOrderConfirmCompletionView.as_view(),
    ),
    path("provider-orders/<str:order_no>/review/", ProviderOrderReviewView.as_view()),
    path("users/me/provider-reviews/", CurrentUserProviderReviewListView.as_view()),
    path("provider-orders/<str:order_no>/simulate-payment/", ProviderOrderSimulatePaymentView.as_view()),
]
