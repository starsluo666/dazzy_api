from django.urls import path

from .views import ProviderListView

urlpatterns = [path("providers/", ProviderListView.as_view(), name="provider-list")]
