from django.contrib.gis.db.models.functions import Distance
from django.db.models import CharField, DateTimeField, OuterRef, Q, Subquery, Value
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backoffice.operation_settings import platform_operation_rules
from config.api import paginated_response
from config.geospatial import gcj02_to_wgs84
from config.payment_capabilities import ensure_activity_real_payment_available
from orders.wechat_oauth import (
    WechatOAuthConfigurationError,
    build_payment_authorization,
    get_official_account_openid,
)

from .huifu import (
    confirm_activity_huifu_payment_status,
    create_activity_huifu_payment_session,
)
from .models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
)
from .selectors import upcoming_public_activities, with_participant_count
from .services import (
    cancel_activity_by_organizer,
    cancel_activity_participation,
    create_activity_after_sales_case,
    create_activity_report,
    get_or_create_participation_order,
    simulate_participation_payment,
)
from .services import create_activity_draft, get_or_create_publish_order, simulate_publish_payment
from .serializers import (
    ActivityDetailSerializer,
    ActivityAfterSalesCaseSerializer,
    ActivityAfterSalesCreateSerializer,
    ActivityListItemSerializer,
    ActivityListQuerySerializer,
    ActivityParticipationSerializer,
    ActivityParticipationCancellationSerializer,
    ActivityOrganizerCancellationSerializer,
    ActivityParticipationCheckoutSerializer,
    ActivityParticipationOrderCreateSerializer,
    ActivityParticipationPaymentOrderSerializer,
    ActivityParticipationRefundOrderSerializer,
    ActivityCategorySerializer,
    ActivityCopySourceSerializer,
    ActivityCreateSerializer,
    ActivityPublishOrderSerializer,
    ActivityReportCreateSerializer,
    ActivityReportReceiptSerializer,
    MyActivityListItemSerializer,
    MyActivityListQuerySerializer,
)


class ActivityListView(APIView):
    permission_classes = [AllowAny]

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
        serializer = ActivityCreateSerializer(data=request.data, context={"request": request})
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
    permission_classes = [AllowAny]

    def get(self, request):
        categories = ActivityCategory.objects.filter(is_active=True).order_by("sort_order", "id")
        if city_code := request.query_params.get("city_code", "").strip():
            categories = categories.filter(Q(city_codes=[]) | Q(city_codes__contains=[city_code]))
        return Response({"data": {"items": ActivityCategorySerializer(categories, many=True).data}})


class ActivityPublishRuleView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        rules = platform_operation_rules()
        return Response(
            {
                "data": {
                    "minimum_advance_hours": rules["activity_minimum_advance_hours"],
                    "maximum_advance_days": rules["activity_maximum_advance_days"],
                }
            }
        )


class ActivityDetailView(APIView):
    permission_classes = [AllowAny]

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
                    "category", "organizer", "organizer__provider_profile", "cover",
                    "settlement",
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
            Activity.objects.select_related("category", "organizer", "cover", "settlement")
        )

        if params["role"] == "joined":
            user_participations = ActivityParticipation.objects.filter(
                activity_id=OuterRef("pk"), user=request.user
            )
            user_refunds = ActivityParticipationRefundOrder.objects.filter(
                activity_id=OuterRef("pk"), beneficiary=request.user
            ).order_by("-created_at")
            user_after_sales = ActivityAfterSalesCase.objects.filter(
                participation__activity_id=OuterRef("pk"), applicant=request.user
            ).order_by("-created_at")
            queryset = queryset.filter(
                pk__in=ActivityParticipation.objects.filter(user=request.user).values(
                    "activity_id"
                )
            ).annotate(
                participation_status=Subquery(user_participations.values("status")[:1]),
                joined_at=Subquery(user_participations.values("joined_at")[:1]),
                participation_payment_expires_at=Subquery(
                    user_participations.values("payment_expires_at")[:1]
                ),
                participation_refund_status=Subquery(user_refunds.values("status")[:1]),
                participation_after_sales_status=Subquery(
                    user_after_sales.values("status")[:1]
                ),
            )
        else:
            queryset = queryset.filter(organizer=request.user).annotate(
                participation_status=Value(None, output_field=CharField()),
                joined_at=Value(None, output_field=DateTimeField()),
                participation_payment_expires_at=Value(
                    None, output_field=DateTimeField()
                ),
                participation_refund_status=Value(None, output_field=CharField()),
                participation_after_sales_status=Value(
                    None, output_field=CharField()
                ),
            )

        now = timezone.now()
        terminal_statuses = (
            Activity.Status.REJECTED,
            Activity.Status.COMPLETED,
            Activity.Status.CANCELLED,
            Activity.Status.FAILED_TO_FORM,
        )
        if params["state"] == "upcoming":
            queryset = queryset.filter(ends_at__gte=now).exclude(status__in=terminal_statuses)
            if params["role"] == "joined":
                queryset = queryset.filter(
                    participation_status__in=(
                        ActivityParticipation.Status.PENDING_PAYMENT,
                        ActivityParticipation.Status.ACTIVE,
                    )
                )
        elif params["state"] == "history":
            history_filter = Q(ends_at__lt=now) | Q(status__in=terminal_statuses)
            if params["role"] == "joined":
                history_filter |= Q(
                    participation_status__in=(
                        ActivityParticipation.Status.CANCELLED,
                        ActivityParticipation.Status.EXPIRED,
                    )
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
        serializer = ActivityParticipationOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        participation, order, created = get_or_create_participation_order(
            activity_id=pk,
            user=request.user,
            channel=serializer.validated_data["channel"],
        )
        participant_count = ActivityParticipation.objects.filter(
            activity_id=pk, status=ActivityParticipation.Status.ACTIVE
        ).count()
        occupied_count = ActivityParticipation.objects.filter(activity_id=pk).filter(
            Q(status=ActivityParticipation.Status.ACTIVE)
            | Q(
                status=ActivityParticipation.Status.PENDING_PAYMENT,
                payment_expires_at__gt=timezone.now(),
            )
        ).count()
        capacity = Activity.objects.only("capacity").get(pk=pk).capacity
        return Response(
            {
                "data": ActivityParticipationCheckoutSerializer({
                    "participation": participation,
                    "order": order,
                    "participant_count": participant_count,
                    "remaining_capacity": max(0, capacity - occupied_count),
                }).data
            },
            status=201 if created else 200,
        )

    def delete(self, request, pk):
        serializer = ActivityParticipationCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        participation, refund, changed = cancel_activity_participation(
            pk, request.user, serializer.validated_data["reason"]
        )
        return Response({"data": {
            "participation": ActivityParticipationSerializer(participation).data,
            "refund": (
                ActivityParticipationRefundOrderSerializer(refund).data if refund else None
            ),
            "changed": changed,
        }})


class ActivityParticipationPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not settings.DEBUG:
            raise ValidationError("模拟支付仅在本地环境开放。")
        participation, order, changed = simulate_participation_payment(
            activity_id=pk, user=request.user
        )
        participant_count = ActivityParticipation.objects.filter(
            activity_id=pk, status=ActivityParticipation.Status.ACTIVE
        ).count()
        return Response({"data": {
            "participation": ActivityParticipationSerializer(participation).data,
            "payment_order": ActivityParticipationPaymentOrderSerializer(order).data,
            "participant_count": participant_count,
            "activity_status": participation.activity.status,
            "changed": changed,
        }})


class ActivityParticipationPaymentAuthorizationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        order = ActivityParticipationPaymentOrder.objects.filter(
            participation__activity_id=pk,
            payer=request.user,
            status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
            expires_at__gt=timezone.now(),
        ).order_by("-created_at", "-id").first()
        if order is None:
            raise ValidationError({"order": "活动报名支付单不存在或已失效。"})
        ensure_activity_real_payment_available("activity_participation")
        authorization = build_payment_authorization(
            user_id=request.user.pk,
            order_no=str(pk),
            payment_kind="activity_participation",
        )
        return Response({"data": authorization})


class ActivityParticipationPaymentSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        app_id = settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip()
        if not app_id:
            raise WechatOAuthConfigurationError()
        sub_openid = get_official_account_openid(
            user_id=request.user.pk,
            app_id=app_id,
        )
        result, created = create_activity_huifu_payment_session(
            payment_kind="activity_participation",
            activity_id=pk,
            user_id=request.user.pk,
            payment_scene="official_account",
            sub_openid=sub_openid,
        )
        return Response(
            {"data": {"invoke_type": "WECHAT_JSAPI", "pay_info": result.pay_info}},
            status=201 if created else 200,
        )


class ActivityParticipationPaymentStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        result = confirm_activity_huifu_payment_status(
            payment_kind="activity_participation",
            activity_id=pk,
            user_id=request.user.pk,
        )
        order = ActivityParticipationPaymentOrder.objects.filter(
            participation__activity_id=pk,
            payer=request.user,
        ).order_by("-created_at", "-id").first()
        return Response({"data": {
            **result,
            "payment_order": ActivityParticipationPaymentOrderSerializer(order).data,
            "participation_status": order.participation.status,
        }})


class ActivityAfterSalesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        cases = ActivityAfterSalesCase.objects.filter(
            participation__activity_id=pk, applicant=request.user
        ).select_related("refund_order").order_by("-created_at")
        return Response({"data": {"items": ActivityAfterSalesCaseSerializer(cases, many=True).data}})

    def post(self, request, pk):
        serializer = ActivityAfterSalesCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        evidence_assets = serializer.validated_data.get("evidence_asset_ids", [])
        case, created = create_activity_after_sales_case(
            activity_id=pk,
            applicant=request.user,
            reason=serializer.validated_data["reason"],
            description=serializer.validated_data["description"],
            evidence_object_keys=[asset.object_key for asset in evidence_assets],
        )
        return Response(
            {"data": ActivityAfterSalesCaseSerializer(case).data},
            status=201 if created else 200,
        )


class ActivityOrganizerCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = ActivityOrganizerCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity, refund = cancel_activity_by_organizer(
            activity_id=pk,
            organizer=request.user,
            reason=serializer.validated_data["reason"],
        )
        return Response({"data": {
            "activity_id": activity.pk,
            "status": activity.status,
            "refund_no": refund.refund_no,
            "refund_amount": refund.refund_amount,
        }})


class ActivityPublishOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        order = get_or_create_publish_order(activity_id=pk, user=request.user)
        return Response({"data": ActivityPublishOrderSerializer(order).data}, status=201)


class ActivityPublishPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not settings.DEBUG:
            raise ValidationError("模拟支付仅在本地环境开放。")
        order = simulate_publish_payment(activity_id=pk, user=request.user)
        return Response({"data": ActivityPublishOrderSerializer(order).data})


class ActivityPublishPaymentAuthorizationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        order = ActivityPublishOrder.objects.filter(
            activity_id=pk,
            payer=request.user,
            status=ActivityPublishOrder.Status.PENDING_PAYMENT,
            expires_at__gt=timezone.now(),
        ).order_by("-created_at", "-id").first()
        if order is None:
            raise ValidationError({"order": "活动发布支付单不存在或已失效。"})
        ensure_activity_real_payment_available("activity_publish")
        authorization = build_payment_authorization(
            user_id=request.user.pk,
            order_no=str(pk),
            payment_kind="activity_publish",
        )
        return Response({"data": authorization})


class ActivityPublishPaymentSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        app_id = settings.WECHAT_OFFICIAL_ACCOUNT_APP_ID.strip()
        if not app_id:
            raise WechatOAuthConfigurationError()
        sub_openid = get_official_account_openid(
            user_id=request.user.pk,
            app_id=app_id,
        )
        result, created = create_activity_huifu_payment_session(
            payment_kind="activity_publish",
            activity_id=pk,
            user_id=request.user.pk,
            payment_scene="official_account",
            sub_openid=sub_openid,
        )
        return Response(
            {"data": {"invoke_type": "WECHAT_JSAPI", "pay_info": result.pay_info}},
            status=201 if created else 200,
        )


class ActivityPublishPaymentStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        result = confirm_activity_huifu_payment_status(
            payment_kind="activity_publish",
            activity_id=pk,
            user_id=request.user.pk,
        )
        order = ActivityPublishOrder.objects.filter(
            activity_id=pk,
            payer=request.user,
        ).order_by("-created_at", "-id").first()
        return Response({"data": {
            **result,
            "publish_order": ActivityPublishOrderSerializer(order).data,
            "activity_status": order.activity.status,
        }})


class ActivityCopySourceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        activity = get_object_or_404(
            Activity.objects.select_related("category", "cover"),
            pk=pk,
            organizer=request.user,
            status=Activity.Status.REJECTED,
        )
        return Response({"data": ActivityCopySourceSerializer(activity).data})


class ActivityReportCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = ActivityReportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report, created = create_activity_report(
            activity_id=pk,
            reporter=request.user,
            reason=serializer.validated_data["reason"],
            description=serializer.validated_data.get("description", ""),
        )
        return Response(
            {"data": ActivityReportReceiptSerializer(report).data},
            status=201 if created else 200,
        )
