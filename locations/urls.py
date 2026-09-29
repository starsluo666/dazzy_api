from django.urls import path

from .views import (
    DiscoveryCityListView, DiscoveryLocateView, PlaceSearchView, ReverseGeocodeView,
    UserAddressDetailView, UserAddressListCreateView,
)

urlpatterns = [
    path("locations/cities/", DiscoveryCityListView.as_view()),
    path("locations/locate/", DiscoveryLocateView.as_view()),
    path("locations/search/", PlaceSearchView.as_view()),
    path("locations/reverse-geocode/", ReverseGeocodeView.as_view()),
    path("addresses/", UserAddressListCreateView.as_view()),
    path("addresses/<int:address_id>/", UserAddressDetailView.as_view()),
]
