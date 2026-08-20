from django.contrib.gis.db.models.functions import Distance
from django.db.models import CharField, DateTimeField, OuterRef, Q, Subquery, Value
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from config.api import paginated_response
from config.geospatial import gcj02_to_wgs84

from .models import Activity, ActivityCategory, ActivityParticipation
from .selectors import upcoming_public_activities, with_participant_count
from .services import cancel_activity_participation, join_activity
from .services import create_activity_draft
from .serializers import (
    ActivityDetailSerializer,
    ActivityListItemSerializer,
    ActivityListQuerySerializer,
    ActivityParticipationSerializer,
    ActivityCategorySerializer,
    ActivityCreateSerializer,
    MyActivityListItemSerializer,
    MyActivityListQuerySerializer,
)


class ActivityListView(APIView):
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

    def post(self, request):
        if not request.user.is_authenticated:
            return Response({"detail": "请登录后发布活动。"}, status=403)
        if request.user.verification_status != request.user.VerificationStatus.VERIFIED:
            return Response({"detail": "完成实名认证后才能发布活动。"}, status=403)
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = create_activity_draft(
            organizer=request.user, validated_data=dict(serializer.validated_data)
        )
        return Response(
            {
                "data": {
                    "id": activity.pk,
                    "status": activity.status,
                    "next_step": "payment",
                    "payment_required": True,
                }
            },
            status=201,
        )


class ActivityCategoryListView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        categories = ActivityCategory.objects.filter(is_active=True).order_by("sort_order", "id")
        return Response({"data": {"items": ActivityCategorySerializer(categories, many=True).data}})


class ActivityDetailView(APIView):
    permission_classes = []

    def get(self, request, pk):
        visible_statuses = (
            Activity.Status.RECRUITING,
            Activity.Status.FORMED,
            Activity.Status.IN_PROGRESS,
            Activity.Status.COMPLETED,
            Activity.Status.CANCELLED,
            Activity.Status.FAILED_TO_FORM,
        )
        visibility = Q(status__in=visible_statuses)
        if request.user.is_authenticated:
            visibility |= Q(organizer=request.user)
        activity = get_object_or_404(
            with_participant_count(
                Activity.objects.select_related(
                    "category", "organizer", "organizer__provider_profile", "cover"
                )
            ).filter(visibility),
            pk=pk,
        )
        return Response(
            {"data": ActivityDetailSerializer(activity, context={"request": request}).data}
        )


class MyActivityListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[MyActivityListQuerySerializer],
        responses={200: MyActivityListItemSerializer(many=True)},
    )
    def get(self, request):
        query = MyActivityListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = with_participant_count(
            Activity.objects.select_related("category", "organizer", "cover")
        )

        if params["role"] == "joined":
            user_participations = ActivityParticipation.objects.filter(
                activity_id=OuterRef("pk"), user=request.user
            )
            queryset = queryset.filter(
                pk__in=ActivityParticipation.objects.filter(user=request.user).values(
                    "activity_id"
                )
            ).annotate(
                participation_status=Subquery(user_participations.values("status")[:1]),
                joined_at=Subquery(user_participations.values("joined_at")[:1]),
            )
        else:
            queryset = queryset.filter(organizer=request.user).annotate(
                participation_status=Value(None, output_field=CharField()),
                joined_at=Value(None, output_field=DateTimeField()),
            )

        now = timezone.now()
        terminal_statuses = (
            Activity.Status.COMPLETED,
            Activity.Status.CANCELLED,
            Activity.Status.FAILED_TO_FORM,
        )
        if params["state"] == "upcoming":
            queryset = queryset.filter(ends_at__gte=now).exclude(status__in=terminal_statuses)
            if params["role"] == "joined":
                queryset = queryset.filter(
                    participation_status=ActivityParticipation.Status.ACTIVE
                )
        elif params["state"] == "history":
            history_filter = Q(ends_at__lt=now) | Q(status__in=terminal_statuses)
            if params["role"] == "joined":
                history_filter |= Q(
                    participation_status=ActivityParticipation.Status.CANCELLED
                )
                queryset = queryset.filter(history_filter)
            else:
                queryset = queryset.filter(history_filter)

        page = params["page"]
        page_size = params["page_size"]
        return paginated_response(
            queryset.order_by("-starts_at", "-id"),
            MyActivityListItemSerializer,
            page=page,
            page_size=page_size,
        )


class ActivityParticipationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        participation, participant_count, created = join_activity(pk, request.user)
        return Response(
            {
                "data": {
                    **ActivityParticipationSerializer(participation).data,
                    "participant_count": participant_count,
                    "activity_status": participation.activity.status,
                }
            },
            status=201 if created else 200,
        )

    def delete(self, request, pk):
        cancel_activity_participation(pk, request.user)
        return Response(status=204)
