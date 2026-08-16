from django.urls import path

from .views import ProviderAvailabilityView, ProviderDetailView, ProviderListView

urlpatterns = [
    path("providers/", ProviderListView.as_view(), name="provider-list"),
    path(
        "providers/<uuid:public_id>/",
        ProviderDetailView.as_view(),
        name="provider-detail",
    ),
    path(
        "providers/<uuid:public_id>/availability/",
        ProviderAvailabilityView.as_view(),
        name="provider-availability",
    ),
]
