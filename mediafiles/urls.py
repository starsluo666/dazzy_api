from django.urls import path

from .views import HomeCardAssetView

urlpatterns = [path("content/home-cards/", HomeCardAssetView.as_view(), name="home-card-assets")]
