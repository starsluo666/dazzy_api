from django.urls import path

from .views import ActivityCategoryListView, ActivityDetailView, ActivityListView, ActivityParticipationView, ActivityPublishOrderView, ActivityPublishPaymentView, MyActivityListView

urlpatterns = [
    path("activities/", ActivityListView.as_view(), name="activity-list"),
    path("activity-categories/", ActivityCategoryListView.as_view(), name="activity-category-list"),
    path("activities/mine/", MyActivityListView.as_view(), name="my-activity-list"),
    path("activities/<int:pk>/", ActivityDetailView.as_view(), name="activity-detail"),
    path(
        "activities/<int:pk>/participation/",
        ActivityParticipationView.as_view(),
        name="activity-participation",
    ),
    path("activities/<int:pk>/publish-order/", ActivityPublishOrderView.as_view(), name="activity-publish-order"),
    path("activities/<int:pk>/publish-order/simulate-payment/", ActivityPublishPaymentView.as_view(), name="activity-publish-payment"),
]
