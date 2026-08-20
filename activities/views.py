from django.contrib.gis.db.models.functions import Distance
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api import paginated_response
from config.geospatial import gcj02_to_wgs84

from .models import Activity
from .selectors import upcoming_public_activities
from .serializers import (
    ActivityDetailSerializer,
    ActivityListItemSerializer,
    ActivityListQuerySerializer,
)


class ActivityListView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(
        parameters=[ActivityListQuerySerializer],
        responses={200: ActivityListItemSerializer(many=True)},
    )
    def get(self, request):
        query = ActivityListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        queryset = upcoming_public_activities()
        if category := params.get("category"):
            queryset = queryset.filter(category__slug=category)
        if "longitude" in params:
            point = gcj02_to_wgs84(params["longitude"], params["latitude"])
            queryset = queryset.annotate(distance=Distance("meeting_point", point))

        ordering = params["ordering"]
        if ordering == "distance":
            queryset = queryset.order_by("distance", "starts_at", "id")
        elif ordering == "latest":
            queryset = queryset.order_by("-published_at", "id")
        else:
            queryset = queryset.order_by("starts_at", "id")

        page = params["page"]
        page_size = params["page_size"]
        return paginated_response(
            queryset,
            ActivityListItemSerializer,
            page=page,
            page_size=page_size,
        )


class ActivityDetailView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, pk):
        activity = get_object_or_404(
            Activity.objects.select_related(
                "category", "organizer", "organizer__provider_profile", "cover"
            ),
            pk=pk,
        )
        return Response({"data": ActivityDetailSerializer(activity).data})
