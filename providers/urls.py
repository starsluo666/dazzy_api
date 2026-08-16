from django.urls import path

from .views import ProviderDetailView, ProviderListView

urlpatterns = [
    path("providers/", ProviderListView.as_view(), name="provider-list"),
    path(
        "providers/<uuid:public_id>/",
        ProviderDetailView.as_view(),
        name="provider-detail",
    ),
]
