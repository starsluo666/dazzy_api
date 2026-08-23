from django.contrib.gis.db.models.functions import Distance
from django.db.models import Min, Q, Sum
from datetime import date, time, timedelta
from django.utils import timezone
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.views import APIView

from config.api import paginated_response
from config.geospatial import gcj02_to_wgs84
from mediafiles.services import build_media_url
from orders.models import ProviderOrder

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
    ProviderApplicationSerializer,
    ProviderApplicationSubmitSerializer,
    ProviderServiceManageSerializer,
    ServiceCategorySerializer,
    ProviderAcceptingOrdersSerializer,
    ProviderDateClosureSerializer,
    ProviderScheduleCreateSerializer,
    ProviderScheduleQuerySerializer,
)
from .services import (
    create_provider_schedule_periods,
    save_provider_application,
    submit_provider_application,
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

        queryset = public_providers().annotate(
            starting_price_amount=Min("services__price_amount", filter=Q(services__is_active=True))
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
    permission_classes = []

    def get(self, request, public_id):
        queryset = public_providers().filter(user__public_id=public_id)
        provider = get_object_or_404(queryset)
        return Response(
            {"data": ProviderDetailSerializer(provider, context={"request": request}).data}
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
        raise PermissionDenied("仅审核通过的达人可以使用达人工作台。")
    return profile


class CurrentProviderWorkbenchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider = current_approved_provider(request)
        today = timezone.localdate()
        day_start = _local_datetime(today, time.min)
        day_end = day_start + timedelta(days=1)
        month_start = day_start.replace(day=1)
        orders = ProviderOrder.objects.filter(provider=provider)
        today_count = (
            orders.filter(starts_at__gte=day_start, starts_at__lt=day_end)
            .exclude(status__in=(ProviderOrder.Status.CANCELLED, ProviderOrder.Status.REFUNDED))
            .count()
        )
        income = (
            orders.filter(paid_at__isnull=False, paid_at__gte=month_start).aggregate(
                total=Sum("service_fee_amount")
            )["total"]
            or 0
        )
        upcoming = (
            orders.filter(starts_at__gte=timezone.now())
            .exclude(status__in=(ProviderOrder.Status.CANCELLED, ProviderOrder.Status.REFUNDED))
            .order_by("starts_at")
            .first()
        )
        upcoming_data = None
        if upcoming:
            upcoming_data = {
                "order_no": upcoming.order_no,
                "starts_at": upcoming.starts_at,
                "ends_at": upcoming.ends_at,
                "service_name": upcoming.service_name_snapshot,
            }
        return Response(
            {
                "data": {
                    "nickname": provider.user.nickname,
                    "avatar_url": build_media_url(provider.user.avatar_object_key),
                    "is_accepting_orders": provider.is_accepting_orders,
                    "admin_order_restricted": provider.admin_order_restricted,
                    "admin_restriction_reason": provider.admin_restriction_reason,
                    "today_order_count": today_count,
                    "month_income_amount": income,
                    "service_count": provider.service_count,
                    "upcoming_order": upcoming_data,
                }
            }
        )

    def patch(self, request):
        provider = current_approved_provider(request)
        serializer = ProviderAcceptingOrdersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data["is_accepting_orders"] and provider.admin_order_restricted:
            raise ValidationError({"is_accepting_orders": "平台当前限制接单，请联系客服处理。"})
        provider.is_accepting_orders = serializer.validated_data["is_accepting_orders"]
        provider.save(update_fields=("is_accepting_orders", "updated_at"))
        return Response({"data": {"is_accepting_orders": provider.is_accepting_orders}})


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
