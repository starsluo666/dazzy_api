from django.urls import path

from .views import (
    ProviderOrderCancelView,
    ProviderOrderDetailView,
    ProviderOrderListCreateView,
    ProviderOrderPreviewView,
    ProviderOrderSimulatePaymentView,
)

urlpatterns = [
    path("provider-orders/preview/", ProviderOrderPreviewView.as_view()),
    path("provider-orders/", ProviderOrderListCreateView.as_view()),
    path("provider-orders/<str:order_no>/", ProviderOrderDetailView.as_view()),
    path("provider-orders/<str:order_no>/cancel/", ProviderOrderCancelView.as_view()),
    path("provider-orders/<str:order_no>/simulate-payment/", ProviderOrderSimulatePaymentView.as_view()),
]
