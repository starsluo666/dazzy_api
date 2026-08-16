from django.contrib.gis.db.models.functions import Distance
from django.db.models import Min, Q
from django.utils import timezone
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api import paginated_response
from config.geospatial import gcj02_to_wgs84

from .selectors import public_providers
from .availability import build_availability
from .models import ProviderService
from .serializers import (
    ProviderAvailabilityQuerySerializer,
    ProviderDetailSerializer,
    ProviderListItemSerializer,
    ProviderListQuerySerializer,
)


class ProviderListView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(
        parameters=[ProviderListQuerySerializer],
        responses={200: ProviderListItemSerializer(many=True)},
    )
    def get(self, request):
        query = ProviderListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        queryset = (
            public_providers()
            .annotate(
                starting_price_amount=Min(
                    "services__price_amount", filter=Q(services__is_active=True)
                )
            )
        )
        if category := params.get("category"):
            queryset = queryset.filter(services__category__slug=category, services__is_active=True)
        if city_code := params.get("city_code"):
            queryset = queryset.filter(service_city_code=city_code)

        if "longitude" in params:
            point = gcj02_to_wgs84(params["longitude"], params["latitude"])
            queryset = queryset.exclude(service_center=None).annotate(
                distance=Distance("service_center", point)
            )

        ordering = params["ordering"]
        if ordering == "distance":
            queryset = queryset.order_by("distance", "-rating", "id")
        elif ordering == "rating":
            queryset = queryset.order_by("-rating", "-service_count", "id")
        elif ordering == "price":
            queryset = queryset.order_by("starting_price_amount", "-rating", "id")
        else:
            queryset = queryset.order_by("-rating", "-service_count", "id")

        queryset = queryset.distinct()
        page = params["page"]
        page_size = params["page_size"]
        return paginated_response(
            queryset,
            ProviderListItemSerializer,
            page=page,
            page_size=page_size,
        )


class ProviderDetailView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, public_id):
        queryset = public_providers().filter(user__public_id=public_id)
        provider = get_object_or_404(queryset)
        return Response({"data": ProviderDetailSerializer(provider).data})


class ProviderAvailabilityView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(parameters=[ProviderAvailabilityQuerySerializer])
    def get(self, request, public_id):
        provider = get_object_or_404(public_providers(), user__public_id=public_id)
        query = ProviderAvailabilityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        service = get_object_or_404(
            ProviderService,
            id=params["service_id"],
            provider=provider,
            is_active=True,
        )
        duration = params.get("duration_minutes")
        if service.billing_type == ProviderService.BillingType.PER_SESSION:
            duration = service.estimated_duration_minutes or 180
        else:
            duration = max(120, duration or service.estimated_duration_minutes or 120)
        start_date = params.get("start_date") or timezone.localdate()
        availability = build_availability(provider, start_date, params["days"], duration)
        return Response(
            {
                "data": {
                    "service_id": service.id,
                    "duration_minutes": duration,
                    "time_grain_minutes": 30,
                    **availability,
                }
            }
        )
