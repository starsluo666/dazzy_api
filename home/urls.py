from django.urls import path

from .views import HomeDiscoveryView

urlpatterns = [path("home/", HomeDiscoveryView.as_view(), name="home-discovery")]
