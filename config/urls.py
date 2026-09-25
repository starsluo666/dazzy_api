from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("api/v1/", include("health.urls")),
    path("api/v1/", include("accounts.urls")),
    path("api/v1/", include("providers.urls")),
    path("api/v1/", include("orders.urls")),
    path("api/v1/", include("activities.urls")),
    path("api/v1/", include("mediafiles.urls")),
    path("api/v1/", include("locations.urls")),
    path("api/v1/", include("engagements.urls")),
    path("api/v1/", include("home.urls")),
    path("api/v1/", include("supportcases.urls")),
    path("api/v1/", include("notifications.urls")),
    path("api/v1/", include("growth.urls")),
    path("api/v1/", include("wallets.urls")),
    path("api/v1/admin/", include("growth.admin_urls")),
    path("api/v1/admin/", include("wallets.admin_urls")),
    path("api/v1/admin/", include("supportcases.admin_urls")),
    path("api/v1/admin/", include("backoffice.urls")),
]

if settings.DEBUG:
    from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

    urlpatterns += [
        path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
        path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    ]
