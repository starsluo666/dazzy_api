from django.urls import path

from .views import ActivityHistoryRecordView, BrowsingHistoryDetailView, BrowsingHistoryListView, ProviderFavoriteListView, ProviderFavoriteView, ProviderHistoryRecordView

urlpatterns = [
    path("favorites/providers/", ProviderFavoriteListView.as_view()),
    path("providers/<uuid:public_id>/favorite/", ProviderFavoriteView.as_view()),
    path("providers/<uuid:public_id>/history/", ProviderHistoryRecordView.as_view()),
    path("activities/<int:pk>/history/", ActivityHistoryRecordView.as_view()),
    path("browsing-history/", BrowsingHistoryListView.as_view()),
    path("browsing-history/<int:pk>/", BrowsingHistoryDetailView.as_view()),
]
