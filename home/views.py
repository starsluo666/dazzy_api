import logging

from django.contrib.gis.db.models.functions import Distance
from django.db.models import Min, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from activities.selectors import upcoming_public_activities
from config.geospatial import gcj02_to_wgs84
from mediafiles.services import build_home_card_assets
from providers.availability import build_availability
from providers.models import ProviderService
from providers.selectors import public_providers

from .serializers import HomeActivitySerializer, HomeProviderSerializer, HomeQuerySerializer

logger = logging.getLogger(__name__)


def _service_duration(service: ProviderService) -> int:
    if service.billing_type == ProviderService.BillingType.PER_SESSION:
        return service.estimated_duration_minutes or 180
    return max(120, service.estimated_duration_minutes or 120)


def _recommended_providers(params, point, request):
    queryset = public_providers().annotate(
        starting_price_amount=Min(
            "services__price_amount", filter=Q(services__is_active=True)
        )
    )
    if city_code := params.get("city_code"):
        queryset = queryset.filter(service_city_code=city_code)
    if point:
        queryset = queryset.exclude(service_center=None).annotate(
            distance=Distance("service_center", point)
        )
    providers = list(
        queryset.order_by("-rating", "-service_count", "id").distinct()[:4]
    )
    earliest_by_provider = {}
    for provider in providers:
        service = next(iter(provider.services.all()), None)
        if not service:
            continue
        availability = build_availability(
            provider,
            timezone.localdate(),
            days=4,
            duration=_service_duration(service),
        )
        earliest_by_provider[provider.pk] = availability["earliest"]
    return HomeProviderSerializer(
        providers,
        many=True,
        context={"earliest_by_provider": earliest_by_provider, "request": request},
    ).data


def _recommended_activities(point):
    activities = upcoming_public_activities()
    if point:
        activities = activities.annotate(distance=Distance("meeting_point", point))
        activities = activities.order_by("distance", "starts_at", "id")
    else:
        activities = activities.order_by("starts_at", "id")
    return HomeActivitySerializer(list(activities[:3]), many=True).data


class HomeDiscoveryView(APIView):
    permission_classes = []

    @extend_schema(parameters=[HomeQuerySerializer])
    def get(self, request):
        query = HomeQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        point = None
        if "longitude" in params:
            point = gcj02_to_wgs84(params["longitude"], params["latitude"])

        data = {
            "card_assets": {},
            "recommended_activities": [],
            "recommended_providers": [],
            "errors": {},
        }
        loaders = {
            "card_assets": build_home_card_assets,
            "recommended_activities": lambda: _recommended_activities(point),
            "recommended_providers": lambda: _recommended_providers(params, point, request),
        }
        for section, loader in loaders.items():
            try:
                data[section] = loader()
            except Exception:
                logger.exception("Home section failed: %s", section)
                data["errors"][section] = "暂时无法加载"

        return Response({"data": data})
