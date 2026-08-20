from django.urls import path

from .views import PlaceSearchView, ReverseGeocodeView, UserAddressDetailView, UserAddressListCreateView

urlpatterns = [
    path("locations/search/", PlaceSearchView.as_view()),
    path("locations/reverse-geocode/", ReverseGeocodeView.as_view()),
    path("addresses/", UserAddressListCreateView.as_view()),
    path("addresses/<int:address_id>/", UserAddressDetailView.as_view()),
]
