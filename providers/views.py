from datetime import date, time, timedelta

from django.contrib.gis.db.models.functions import Distance
from django.db.models import Count, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView

from config.api import paginated_response
from config.geospatial import gcj02_to_wgs84
from mediafiles.services import build_media_url
from orders.models import ProviderOrder, ProviderOrderReview

from .selectors import public_providers
from .availability import _blocking_orders, _local_datetime, build_availability
from .models import (
    ProviderDateAvailability,
    ProviderDateClosure,
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)
from .serializers import (
    ProviderAvailabilityQuerySerializer,
    ProviderDetailSerializer,
    ProviderListItemSerializer,
    ProviderListQuerySerializer,
    ProviderReviewQuerySerializer,
    PublicProviderReviewSerializer,
    ProviderApplicationSerializer,
    ProviderApplicationSubmitSerializer,
    ProviderServiceManageSerializer,
    ServiceCategorySerializer,
    ProviderDateClosureSerializer,
    ProviderLiveLocationInputSerializer,
    ProviderLiveLocationUpdateSerializer,
    ProviderScheduleCreateSerializer,
    ProviderScheduleQuerySerializer,
)
from .presence import (
    operation_rules,
    get_provider_live_location,
    location_expires_at,
    online_provider_query,
    provider_is_online,
)
from .services import (
    create_provider_schedule_periods,
    save_provider_application,
    start_provider_online,
    stop_provider_online,
    submit_provider_application,
    update_provider_live_location,
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

        queryset = public_providers().filter(online_provider_query()).annotate(
            starting_price_amount=Min(
                "services__price_amount",
                filter=Q(services__is_active=True, services__category__is_active=True),
            )
        )
        if category := params.get("category"):
            queryset = queryset.filter(
                services__category__slug=category,
                services__category__is_active=True,
                services__is_active=True,
            )
        if city_code := params.get("city_code"):
            queryset = queryset.filter(service_city_code=city_code)

        if "longitude" in params:
            point = gcj02_to_wgs84(params["longitude"], params["latitude"])
            queryset = queryset.annotate(distance=Distance("live_location__position", point))

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
    permission_classes = []

    def get(self, request, public_id):
        queryset = public_providers().filter(user__public_id=public_id)
        provider = get_object_or_404(queryset)
        return Response(
            {"data": ProviderDetailSerializer(provider, context={"request": request}).data}
        )


class ProviderReviewListView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, public_id):
        provider = get_object_or_404(public_providers(), user__public_id=public_id)
        query = ProviderReviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        base = ProviderOrderReview.objects.filter(
            provider=provider, is_visible=True
        ).select_related("customer", "order").prefetch_related("images")
        distribution = {str(value): 0 for value in range(1, 6)}
        for item in base.values("rating").annotate(count=Count("id")):
            distribution[str(item["rating"])] = item["count"]
        reviews = base
        if rating := params.get("rating"):
            reviews = reviews.filter(rating=rating)
        page = params["page"]
        page_size = params["page_size"]
        total = reviews.count()
        items = reviews[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": PublicProviderReviewSerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": {
                        "rating": str(provider.rating),
                        "total": sum(distribution.values()),
                        "distribution": distribution,
                    },
                }
            }
        )


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
            category__is_active=True,
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


class ServiceCategoryListView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        categories = ServiceCategory.objects.filter(is_active=True).order_by("sort_order", "id")
        return Response({"data": {"items": ServiceCategorySerializer(categories, many=True).data}})


class CurrentProviderApplicationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = ProviderProfile.objects.filter(user=request.user).first()
        return Response({"data": ProviderApplicationSerializer(profile).data if profile else None})

    def patch(self, request):
        current = ProviderProfile.objects.filter(user=request.user).first()
        serializer = ProviderApplicationSerializer(
            current, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        profile = save_provider_application(user=request.user, data=serializer.validated_data)
        return Response({"data": ProviderApplicationSerializer(profile).data})


class CurrentProviderApplicationSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ProviderApplicationSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = submit_provider_application(user=request.user)
        return Response({"data": ProviderApplicationSerializer(profile).data})


class CurrentProviderServiceListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def provider(self, request):
        profile = get_object_or_404(ProviderProfile, user=request.user)
        if profile.status != ProviderProfile.Status.APPROVED:
            raise PermissionDenied("仅审核通过的达人可以管理服务。")
        return profile

    def get(self, request):
        services = self.provider(request).services.select_related("category").order_by("id")
        return Response(
            {"data": {"items": ProviderServiceManageSerializer(services, many=True).data}}
        )

    def post(self, request):
        provider = self.provider(request)
        serializer = ProviderServiceManageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = serializer.save(provider=provider)
        return Response(
            {"data": ProviderServiceManageSerializer(service).data}, status=status.HTTP_201_CREATED
        )


class CurrentProviderServiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, service_id):
        return get_object_or_404(
            ProviderService.objects.select_related("category"),
            id=service_id,
            provider__user=request.user,
            provider__status=ProviderProfile.Status.APPROVED,
        )

    def patch(self, request, service_id):
        service = self.get_object(request, service_id)
        serializer = ProviderServiceManageSerializer(service, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": serializer.data})

    def delete(self, request, service_id):
        service = self.get_object(request, service_id)
        service.is_active = False
        service.save(update_fields=("is_active", "updated_at"))
        return Response(status=status.HTTP_204_NO_CONTENT)


def current_approved_provider(request):
    profile = get_object_or_404(ProviderProfile.objects.select_related("user"), user=request.user)
    if profile.status != ProviderProfile.Status.APPROVED:
        raise PermissionDenied("仅审核通过的达人可以使用达人端功能。")
    return profile


class CurrentProviderWorkbenchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = current_approved_provider(request)
        location = get_provider_live_location(provider)
        today = timezone.localdate()
        day_start = _local_datetime(today, time.min)
        day_end = day_start + timedelta(days=1)
        month_start = day_start.replace(day=1)
        orders = ProviderOrder.objects.filter(provider=provider)
        excluded_statuses = (ProviderOrder.Status.CANCELLED, ProviderOrder.Status.REFUNDED)
        fulfilled_statuses = (
            ProviderOrder.Status.PENDING_CONFIRMATION,
            ProviderOrder.Status.PENDING_REVIEW,
            ProviderOrder.Status.COMPLETED,
        )
        today_count = (
            orders.filter(starts_at__gte=day_start, starts_at__lt=day_end)
            .exclude(status__in=excluded_statuses)
            .count()
        )
        pending_acceptance_count = orders.filter(
            status=ProviderOrder.Status.PENDING_ACCEPTANCE,
            paid_at__isnull=False,
        ).count()
        paid_month_orders = orders.filter(
            paid_at__isnull=False,
            paid_at__gte=month_start,
        ).exclude(status__in=excluded_statuses)
        income = (
            paid_month_orders.aggregate(
                total=Sum("service_fee_amount")
            )["total"]
            or 0
        )
        month_order_count = paid_month_orders.count()
        month_service_minutes = (
            orders.filter(
                starts_at__gte=month_start,
                status__in=fulfilled_statuses,
            ).aggregate(total=Sum("duration_minutes"))["total"]
            or 0
        )

        trend_start_date = today - timedelta(days=today.weekday())
        trend_start = _local_datetime(trend_start_date, time.min)
        trend_end = trend_start + timedelta(days=7)
        trend_rows = (
            orders.filter(
                starts_at__gte=trend_start,
                starts_at__lt=trend_end,
                status__in=fulfilled_statuses,
            )
            .annotate(service_date=TruncDate("starts_at", tzinfo=timezone.get_current_timezone()))
            .values("service_date")
            .annotate(total_minutes=Sum("duration_minutes"))
        )
        trend_minutes = {
            row["service_date"]: row["total_minutes"] or 0 for row in trend_rows
        }
        weekday_labels = "一二三四五六日"
        service_trend = []
        for offset in range(7):
            service_date = trend_start_date + timedelta(days=offset)
            service_trend.append(
                {
                    "date": service_date.isoformat(),
                    "label": weekday_labels[service_date.weekday()],
                    "service_hours": round(trend_minutes.get(service_date, 0) / 60, 1),
                }
            )
        upcoming = (
            orders.filter(starts_at__gte=timezone.now(), paid_at__isnull=False)
            .exclude(status__in=excluded_statuses)
            .select_related("customer")
            .order_by("starts_at")
            .first()
        )
        upcoming_data = None
        if upcoming:
            upcoming_data = {
                "public_id": upcoming.public_id,
                "order_no": upcoming.order_no,
                "starts_at": upcoming.starts_at,
                "ends_at": upcoming.ends_at,
                "service_name": upcoming.service_name_snapshot,
                "customer_name": upcoming.contact_name or upcoming.customer.nickname,
                "customer_gender_label": upcoming.get_contact_gender_display(),
                "meeting_location_name": (
                    upcoming.meeting_location_name or upcoming.meeting_address
                ),
                "status": upcoming.status,
            }
        return Response(
            {
                "data": {
                    "nickname": provider.user.nickname,
                    "avatar_url": build_media_url(provider.user.avatar_object_key),
                    "is_accepting_orders": provider.is_accepting_orders,
                    "is_online": provider_is_online(provider),
                    "session_id": (
                        str(location.session_id) if location and location.session_id else None
                    ),
                    "admin_order_restricted": provider.admin_order_restricted,
                    "admin_restriction_reason": provider.admin_restriction_reason,
                    "service_city_code": provider.service_city_code,
                    "service_city_name": provider.service_city_name,
                    "max_service_radius_km": provider.max_service_radius_km,
                    "location_updated_at": location.received_at if location else None,
                    "location_accuracy_m": location.accuracy_m if location else None,
                    "location_expires_at": location_expires_at(location),
                    "online_timeout_minutes": operation_rules()["timeout_minutes"],
                    "recommended_report_interval_seconds": operation_rules()["report_interval_seconds"],
                    "today_order_count": today_count,
                    "pending_acceptance_order_count": pending_acceptance_count,
                    "month_income_amount": income,
                    "month_order_count": month_order_count,
                    "month_service_hours": round(month_service_minutes / 60, 1),
                    "last_7_days_service_trend": service_trend,
                    "service_count": provider.service_count,
                    "upcoming_order": upcoming_data,
                }
            }
        )



def _online_payload(provider, location=None):
    location = location or get_provider_live_location(provider)
    return {
        "is_accepting_orders": provider.is_accepting_orders,
        "is_online": provider_is_online(provider),
        "session_id": str(location.session_id) if location and location.session_id else None,
        "location_updated_at": location.received_at if location else None,
        "location_accuracy_m": location.accuracy_m if location else None,
        "location_expires_at": location_expires_at(location),
        "online_timeout_minutes": operation_rules()["timeout_minutes"],
        "recommended_report_interval_seconds": operation_rules()["report_interval_seconds"],
    }


class CurrentProviderOnlineStartView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        provider = current_approved_provider(request)
        serializer = ProviderLiveLocationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        provider, location = start_provider_online(
            provider=provider,
            data=serializer.validated_data,
        )
        return Response({"data": _online_payload(provider, location)})


class CurrentProviderOnlineLocationView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request):
        provider = current_approved_provider(request)
        serializer = ProviderLiveLocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        session_id = data.pop("session_id")
        provider, location = update_provider_live_location(
            provider=provider,
            session_id=session_id,
            data=data,
        )
        return Response({"data": _online_payload(provider, location)})


class CurrentProviderOnlineStopView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        provider = stop_provider_online(provider=current_approved_provider(request))
        return Response({"data": _online_payload(provider)})


class CurrentProviderScheduleView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = current_approved_provider(request)
        serializer = ProviderScheduleQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        start = serializer.validated_data.get("start_date") or timezone.localdate()
        days = serializer.validated_data["days"]
        end = start + timedelta(days=days)
        weekly = list(provider.weekly_availability.filter(is_active=True))
        extras = list(provider.date_availability.filter(date__gte=start, date__lt=end))
        closures = set(
            provider.date_closures.filter(date__gte=start, date__lt=end).values_list(
                "date", flat=True
            )
        )
        orders = _blocking_orders(
            provider, _local_datetime(start, time.min), _local_datetime(end, time.min)
        )
        result = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            periods = []
            for item in weekly:
                if item.weekday == day.weekday():
                    periods.append(
                        {
                            "id": f"weekly-{item.id}",
                            "source": "weekly",
                            "starts_at": item.starts_at,
                            "ends_at": item.ends_at,
                            "status": "available",
                        }
                    )
            for item in extras:
                if item.date == day:
                    periods.append(
                        {
                            "id": f"date-{item.id}",
                            "source": "date",
                            "starts_at": item.starts_at,
                            "ends_at": item.ends_at,
                            "status": "available",
                        }
                    )
            for order_start, order_end in orders:
                local_start, local_end = (
                    timezone.localtime(order_start),
                    timezone.localtime(order_end),
                )
                if local_start.date() == day:
                    periods.append(
                        {
                            "id": None,
                            "source": "order",
                            "starts_at": local_start.time(),
                            "ends_at": local_end.time(),
                            "status": "booked",
                        }
                    )
            periods.sort(key=lambda item: item["starts_at"])
            result.append({"date": day, "is_closed": day in closures, "periods": periods})
        return Response({"data": {"start_date": start, "days": result}})

    def post(self, request):
        provider = current_approved_provider(request)
        serializer = ProviderScheduleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created = create_provider_schedule_periods(
            provider=provider,
            data=serializer.validated_data,
        )
        return Response({"data": {"ids": created}}, status=status.HTTP_201_CREATED)


class CurrentProviderSchedulePeriodView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, period_id):
        provider = current_approved_provider(request)
        source, raw_id = period_id.split("-", 1) if "-" in period_id else ("", "")
        model = (
            ProviderWeeklyAvailability
            if source == "weekly"
            else ProviderDateAvailability
            if source == "date"
            else None
        )
        if model is None:
            return Response({"detail": "档期标识无效。"}, status=status.HTTP_400_BAD_REQUEST)
        get_object_or_404(model, id=raw_id, provider=provider).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentProviderScheduleDayView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, day):
        provider = current_approved_provider(request)
        try:
            day = date.fromisoformat(day)
        except ValueError:
            return Response(
                {"detail": "日期格式应为 YYYY-MM-DD。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if day < timezone.localdate():
            return Response(
                {"detail": "不能修改过去日期的档期。"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = ProviderDateClosureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["is_closed"]:
            ProviderDateClosure.objects.get_or_create(provider=provider, date=day)
        else:
            ProviderDateClosure.objects.filter(provider=provider, date=day).delete()
        return Response(
            {"data": {"date": day, "is_closed": serializer.validated_data["is_closed"]}}
        )
