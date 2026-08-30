from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Prefetch, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from activities.models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityCategory,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivityRefundRecord,
    ActivityReport,
    ActivitySettlement,
)
from config.api import paginated_response
from mediafiles.services import build_media_url
from orders.models import ProviderOrder
from providers.models import ProviderProfile, ProviderService, ServiceCategory
from providers.presence import online_provider_query

from .access import client_ip, resolve_admin_access
from .models import (
    AdminAuditLog,
    Organization,
    OrganizationMember,
    ProviderOrderAfterSalesCase,
    ProviderOrderSupportNote,
    ProviderOrderingSetting,
)
from .serializers import (
    AdminActivityQuerySerializer,
    AdminActivityActionSerializer,
    AdminActivityCategoryQuerySerializer,
    AdminActivityCategorySerializer,
    AdminActivityAfterSalesActionSerializer,
    AdminActivityAfterSalesSerializer,
    AdminActivityFinanceQuerySerializer,
    AdminActivityParticipationPaymentSerializer,
    AdminActivityParticipationRefundSerializer,
    AdminActivitySettlementActionSerializer,
    AdminActivitySettlementSerializer,
    AdminActivityReportActionSerializer,
    AdminActivityReportQuerySerializer,
    AdminActivityReportSerializer,
    AdminActivityReviewSerializer,
    AdminActivitySerializer,
    AdminMeSerializer,
    AdminOverviewQuerySerializer,
    AdminOrganizationSerializer,
    AdminServiceCategoryQuerySerializer,
    AdminServiceCategorySerializer,
    AdminUserAccountActionSerializer,
    AdminUserListSerializer,
    AdminUserQuerySerializer,
    AdminUserRiskActionSerializer,
    AuditLogSerializer,
    OrganizationMemberSerializer,
    ProviderAdminActionSerializer,
    ProviderAdminQuerySerializer,
    ProviderAdminSerializer,
    ProviderCreditAdjustmentInputSerializer,
    ProviderOrderAfterSalesCaseActionSerializer,
    ProviderOrderAfterSalesCaseCreateSerializer,
    ProviderOrderAfterSalesCaseQuerySerializer,
    ProviderOrderAfterSalesCaseSerializer,
    ProviderOrderAdminQuerySerializer,
    ProviderOrderAdminSerializer,
    ProviderOrderSupportNoteInputSerializer,
    ProviderOrderSupportNoteSerializer,
    ProviderOrderingSettingSerializer,
    ProviderApplicationQuerySerializer,
    ProviderReviewDecisionSerializer,
    ProviderReviewListSerializer,
)
from .services import (
    adjust_provider_credit,
    change_provider_operational_status,
    change_user_account_status,
    change_user_risk_flag,
    create_provider_order_after_sales_case,
    review_provider_order_after_sales_case,
    review_provider_application,
    review_activity,
    review_activity_after_sales_case,
    review_activity_settlement,
    cancel_activity_by_admin,
    review_activity_report,
)


def scoped_providers(access):
    queryset = ProviderProfile.objects.all()
    if not access.all_data:
        queryset = queryset.filter(service_city_code__in=access.city_codes)
    return queryset


def scoped_users(access):
    queryset = User.objects.filter(
        is_superuser=False,
        backoffice_memberships__isnull=True,
    )
    if not access.all_data:
        queryset = queryset.filter(
            Q(provider_orders__provider__service_city_code__in=access.city_codes)
            | Q(provider_profile__service_city_code__in=access.city_codes)
        )
    return queryset.distinct()


def admin_user_queryset(access):
    return (
        scoped_users(access)
        .select_related(
            "provider_profile",
            "admin_risk_flag__marked_by",
            "admin_risk_flag__cleared_by",
        )
        .annotate(
            order_count=Count("provider_orders", distinct=True),
            activity_count=Count("organized_activities", distinct=True)
            + Count("activity_participations", distinct=True),
        )
    )


def provider_admin_queryset(access):
    return (
        scoped_providers(access)
        .select_related("user", "lifestyle_photo", "live_location")
        .prefetch_related(
            "services__category",
            "weekly_availability",
            "admin_credit_adjustments__operator",
            "admin_credit_adjustments__organization",
        )
    )


def can_access(access, permission):
    return "*" in access.permissions or permission in access.permissions


def service_category_admin_queryset():
    return ServiceCategory.objects.annotate(
        service_count=Count("provider_services", distinct=True),
        active_service_count=Count(
            "provider_services",
            filter=Q(provider_services__is_active=True, is_active=True),
            distinct=True,
        ),
        provider_count=Count("provider_services__provider", distinct=True),
    )


def service_category_audit_snapshot(category):
    return {
        "name": category.name,
        "slug": category.slug,
        "icon_object_key": category.icon_object_key,
        "city_codes": category.city_codes,
        "sort_order": category.sort_order,
        "is_active": category.is_active,
    }


def scoped_activities(access):
    queryset = Activity.objects.all()
    if not access.all_data:
        queryset = queryset.filter(city_code__in=access.city_codes)
    return queryset


def activity_admin_queryset(access):
    return (
        scoped_activities(access)
        .select_related("category", "organizer", "cover", "reviewed_by", "settlement")
        .prefetch_related(
            Prefetch(
                "publish_orders",
                queryset=ActivityPublishOrder.objects.order_by("-created_at"),
            ),
            Prefetch(
                "participations",
                queryset=ActivityParticipation.objects.select_related("user")
                .prefetch_related(
                    Prefetch(
                        "payment_orders",
                        queryset=ActivityParticipationPaymentOrder.objects.order_by(
                            "-created_at"
                        ),
                    ),
                    Prefetch(
                        "refund_orders",
                        queryset=ActivityParticipationRefundOrder.objects.order_by(
                            "-created_at"
                        ),
                    ),
                    Prefetch(
                        "after_sales_cases",
                        queryset=ActivityAfterSalesCase.objects.order_by("-created_at"),
                    ),
                )
                .order_by("-joined_at"),
            ),
            Prefetch(
                "refund_records",
                queryset=ActivityRefundRecord.objects.select_related(
                    "beneficiary", "operator", "publish_order"
                ).order_by("-created_at"),
            ),
        )
        .annotate(
            participant_count=Count(
                "participations",
                filter=Q(participations__status=ActivityParticipation.Status.ACTIVE),
                distinct=True,
            ),
            report_count=Count("reports", distinct=True),
        )
    )


def activity_category_admin_queryset():
    return ActivityCategory.objects.annotate(
        activity_count=Count("activities", distinct=True),
        active_activity_count=Count(
            "activities",
            filter=Q(
                activities__status__in=(
                    Activity.Status.PENDING_REVIEW,
                    Activity.Status.RECRUITING,
                    Activity.Status.FORMED,
                    Activity.Status.IN_PROGRESS,
                )
            ),
            distinct=True,
        ),
    )


def activity_category_audit_snapshot(category):
    return {
        "name": category.name,
        "slug": category.slug,
        "icon_object_key": category.icon_object_key,
        "city_codes": category.city_codes,
        "min_capacity": category.min_capacity,
        "max_capacity": category.max_capacity,
        "min_aa_principal_amount": category.min_aa_principal_amount,
        "max_aa_principal_amount": category.max_aa_principal_amount,
        "content_guidance": category.content_guidance,
        "sort_order": category.sort_order,
        "is_active": category.is_active,
    }


def activity_report_admin_queryset(access):
    queryset = ActivityReport.objects.select_related(
        "activity__organizer", "reporter", "reviewed_by"
    )
    if not access.all_data:
        queryset = queryset.filter(activity__city_code__in=access.city_codes)
    return queryset


def scoped_provider_orders(access):
    queryset = ProviderOrder.objects.all()
    if not access.all_data:
        queryset = queryset.filter(provider__service_city_code__in=access.city_codes)
    return queryset


def order_anomaly_query(code):
    evidence_statuses = (
        ProviderOrder.Status.IN_SERVICE,
        ProviderOrder.Status.PENDING_CONFIRMATION,
        ProviderOrder.Status.PENDING_REVIEW,
        ProviderOrder.Status.COMPLETED,
    )
    missing_evidence = Q(status__in=evidence_statuses, arrival_photo__isnull=True)
    timeline_gap = (
        Q(
            status__in=(ProviderOrder.Status.DEPARTED, *evidence_statuses),
            departed_at__isnull=True,
        )
        | Q(status__in=evidence_statuses, service_started_at__isnull=True)
        | Q(
            status__in=(
                ProviderOrder.Status.PENDING_CONFIRMATION,
                ProviderOrder.Status.PENDING_REVIEW,
                ProviderOrder.Status.COMPLETED,
            ),
            completion_submitted_at__isnull=True,
        )
        | Q(
            status__in=(ProviderOrder.Status.PENDING_REVIEW, ProviderOrder.Status.COMPLETED),
            customer_confirmed_at__isnull=True,
        )
    )
    confirmation_overdue = Q(
        status=ProviderOrder.Status.PENDING_CONFIRMATION,
        completion_submitted_at__lte=timezone.now()
        - timedelta(days=settings.PROVIDER_ORDER_AUTO_CONFIRM_DAYS),
    )
    mapping = {
        "missing_evidence": missing_evidence,
        "timeline_gap": timeline_gap,
        "confirmation_overdue": confirmation_overdue,
    }
    if code == "all":
        return missing_evidence | timeline_gap | confirmation_overdue
    return mapping[code]


def provider_order_queryset(access):
    return scoped_provider_orders(access).select_related(
        "customer", "provider__user", "service__category", "arrival_photo"
    ).prefetch_related(
        "support_notes__author",
        "support_notes__organization",
        "after_sales_cases__creator",
        "after_sales_cases__organization",
        "after_sales_cases__reviewed_by",
    )


def get_admin_provider_order(access, order_no):
    return get_object_or_404(provider_order_queryset(access), order_no=order_no)


def provider_order_after_sales_queryset(access):
    queryset = ProviderOrderAfterSalesCase.objects.select_related(
        "order__customer",
        "order__provider__user",
        "creator",
        "organization",
        "reviewed_by",
    )
    if not access.all_data:
        queryset = queryset.filter(order__provider__service_city_code__in=access.city_codes)
    return queryset


def build_order_trend(orders, *, days):
    today = timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    current_timezone = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(start_date, time.min), current_timezone)
    end_at = timezone.now()

    order_counts = {
        row["date"]: row["count"]
        for row in orders.filter(created_at__gte=start_at, created_at__lte=end_at)
        .annotate(date=TruncDate("created_at", tzinfo=current_timezone))
        .values("date")
        .annotate(count=Count("id"))
        .order_by("date")
    }
    transaction_amounts = {
        row["date"]: row["amount"]
        for row in orders.filter(paid_at__gte=start_at, paid_at__lte=end_at)
        .annotate(date=TruncDate("paid_at", tzinfo=current_timezone))
        .values("date")
        .annotate(amount=Sum("payable_amount"))
        .order_by("date")
    }
    points = []
    for offset in range(days):
        date = start_date + timedelta(days=offset)
        points.append(
            {
                "date": date.isoformat(),
                "transaction_amount": transaction_amounts.get(date, 0),
                "order_count": order_counts.get(date, 0),
            }
        )
    return {
        "days": days,
        "points": points,
    }


class AdminMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        member = access.member
        data = {
            "user": request.user,
            "organization": member.organization if member else None,
            "role_name": member.role.name if member else "超级管理员",
            "permissions": sorted(access.permissions),
            "data_scope": member.role.data_scope if member else "all",
            "city_codes": sorted(access.city_codes),
        }
        return Response({"data": AdminMeSerializer(data).data})


class AdminOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("dashboard.view")
        query = AdminOverviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        days = query.validated_data["days"]
        providers = scoped_providers(access)
        today = timezone.localdate()
        orders = ProviderOrder.objects.all()
        activities = Activity.objects.all()
        if not access.all_data:
            orders = orders.filter(provider__service_city_code__in=access.city_codes)
            activities = activities.filter(city_code__in=access.city_codes)
        trend = build_order_trend(orders, days=days)
        week_transaction_amount = sum(
            point["transaction_amount"] for point in trend["points"][-7:]
        )
        return Response(
            {
                "data": {
                    "metrics": {
                        "today_new_users": User.objects.filter(date_joined__date=today).count()
                        if access.all_data else None,
                        "pending_providers": providers.filter(status=ProviderProfile.Status.PENDING).count(),
                        "active_orders": orders.filter(
                            status__in=(
                                ProviderOrder.Status.PENDING_ACCEPTANCE,
                                ProviderOrder.Status.PENDING_SERVICE,
                                ProviderOrder.Status.DEPARTED,
                                ProviderOrder.Status.IN_SERVICE,
                            )
                        ).count(),
                        "pending_activities": activities.filter(status=Activity.Status.PENDING_REVIEW).count(),
                        "week_transaction_amount": week_transaction_amount,
                    },
                    "trend": trend,
                    "todos": [
                        {
                            "key": "provider_review",
                            "label": "达人入驻审核",
                            "count": providers.filter(status=ProviderProfile.Status.PENDING).count(),
                            "priority": "high",
                        },
                        {
                            "key": "activity_review",
                            "label": "活动发布审核",
                            "count": activities.filter(status=Activity.Status.PENDING_REVIEW).count(),
                            "priority": "medium",
                        },
                    ],
                }
            }
        )


class AdminServiceCategoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("service_category.view")
        query = AdminServiceCategoryQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = service_category_admin_queryset()
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(name__icontains=keyword) | Q(slug__icontains=keyword)
            )
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "active": summary_queryset.filter(is_active=True).count(),
            "inactive": summary_queryset.filter(is_active=False).count(),
            "active_services": ProviderService.objects.filter(
                category__in=summary_queryset,
                category__is_active=True,
                is_active=True,
            ).count(),
        }
        if params["status"] == "active":
            queryset = queryset.filter(is_active=True)
        elif params["status"] == "inactive":
            queryset = queryset.filter(is_active=False)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("sort_order", "id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response(
            {
                "data": {
                    "items": AdminServiceCategorySerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )

    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("service_category.manage")
        serializer = AdminServiceCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            category = serializer.save()
            AdminAuditLog.objects.create(
                actor=request.user,
                organization=access.member.organization if access.member else None,
                action="service_category.create",
                target_type="service_category",
                target_id=str(category.id),
                before={},
                after=service_category_audit_snapshot(category),
                request_id=request.headers.get("X-Request-ID", ""),
                ip_address=client_ip(request),
            )
        category = service_category_admin_queryset().get(pk=category.pk)
        return Response(
            {"data": AdminServiceCategorySerializer(category).data},
            status=201,
        )


class AdminServiceCategoryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, category_id):
        access = resolve_admin_access(request.user)
        access.require("service_category.manage")
        with transaction.atomic():
            category = get_object_or_404(
                ServiceCategory.objects.select_for_update(),
                pk=category_id,
            )
            before = service_category_audit_snapshot(category)
            serializer = AdminServiceCategorySerializer(
                category,
                data=request.data,
                partial=True,
            )
            serializer.is_valid(raise_exception=True)
            category = serializer.save()
            action = "service_category.update"
            if before["is_active"] != category.is_active:
                action = (
                    "service_category.enable"
                    if category.is_active
                    else "service_category.disable"
                )
            AdminAuditLog.objects.create(
                actor=request.user,
                organization=access.member.organization if access.member else None,
                action=action,
                target_type="service_category",
                target_id=str(category.id),
                before=before,
                after=service_category_audit_snapshot(category),
                request_id=request.headers.get("X-Request-ID", ""),
                ip_address=client_ip(request),
            )
        category = service_category_admin_queryset().get(pk=category.pk)
        return Response({"data": AdminServiceCategorySerializer(category).data})


class AdminActivityListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("activity.view")
        query = AdminActivityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = activity_admin_queryset(access)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(title__icontains=keyword)
                | Q(organizer__nickname__icontains=keyword)
                | Q(organizer__phone__icontains=keyword)
                | Q(meeting_place_name__icontains=keyword)
            )
        if city_code := params.get("city_code", "").strip():
            queryset = queryset.filter(city_code=city_code)
        if category := params.get("category", "").strip():
            queryset = queryset.filter(category__slug=category)

        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "pending_review": summary_queryset.filter(
                status=Activity.Status.PENDING_REVIEW
            ).count(),
            "active": summary_queryset.filter(
                status__in=(
                    Activity.Status.RECRUITING,
                    Activity.Status.FORMED,
                    Activity.Status.IN_PROGRESS,
                )
            ).count(),
            "ended": summary_queryset.filter(
                status__in=(
                    Activity.Status.REJECTED,
                    Activity.Status.COMPLETED,
                    Activity.Status.CANCELLED,
                    Activity.Status.FAILED_TO_FORM,
                )
            ).count(),
        }
        if status_value := params.get("status", ""):
            queryset = queryset.filter(status=status_value)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-created_at", "-id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response(
            {
                "data": {
                    "items": AdminActivitySerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )


class AdminActivityDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, activity_id):
        access = resolve_admin_access(request.user)
        access.require("activity.view")
        activity = get_object_or_404(activity_admin_queryset(access), id=activity_id)
        return Response(
            {"data": AdminActivitySerializer(
                activity, context={"include_detail": True}
            ).data}
        )


class AdminActivityReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, activity_id):
        access = resolve_admin_access(request.user)
        access.require("activity.review")
        serializer = AdminActivityReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = review_activity(
            activity_id=activity_id,
            decision=serializer.validated_data["decision"],
            reason=serializer.validated_data.get("reason", ""),
            actor=request.user,
            access=access,
            request=request,
        )
        activity = get_object_or_404(activity_admin_queryset(access), id=activity.id)
        return Response(
            {"data": AdminActivitySerializer(
                activity, context={"include_detail": True}
            ).data}
        )


class AdminActivityActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, activity_id):
        access = resolve_admin_access(request.user)
        access.require("activity.manage")
        serializer = AdminActivityActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = cancel_activity_by_admin(
            activity_id=activity_id,
            reason=serializer.validated_data["reason"],
            actor=request.user,
            access=access,
            request=request,
        )
        activity = get_object_or_404(activity_admin_queryset(access), id=activity.id)
        return Response(
            {"data": AdminActivitySerializer(
                activity, context={"include_detail": True}
            ).data}
        )


class AdminActivityCategoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("activity_category.view")
        query = AdminActivityCategoryQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = activity_category_admin_queryset()
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(name__icontains=keyword) | Q(slug__icontains=keyword)
            )
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "active": summary_queryset.filter(is_active=True).count(),
            "inactive": summary_queryset.filter(is_active=False).count(),
            "active_activities": Activity.objects.filter(
                category__in=summary_queryset,
                status__in=(
                    Activity.Status.PENDING_REVIEW,
                    Activity.Status.RECRUITING,
                    Activity.Status.FORMED,
                    Activity.Status.IN_PROGRESS,
                ),
            ).count(),
        }
        if params["status"] == "active":
            queryset = queryset.filter(is_active=True)
        elif params["status"] == "inactive":
            queryset = queryset.filter(is_active=False)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("sort_order", "id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response({"data": {
            "items": AdminActivityCategorySerializer(items, many=True).data,
            "pagination": {"page": page, "page_size": page_size, "total": total},
            "summary": summary,
        }})

    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("activity_category.manage")
        serializer = AdminActivityCategorySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            category = serializer.save()
            AdminAuditLog.objects.create(
                actor=request.user,
                organization=access.member.organization if access.member else None,
                action="activity_category.create",
                target_type="activity_category",
                target_id=str(category.id),
                before={},
                after=activity_category_audit_snapshot(category),
                request_id=request.headers.get("X-Request-ID", ""),
                ip_address=client_ip(request),
            )
        category = activity_category_admin_queryset().get(pk=category.pk)
        return Response(
            {"data": AdminActivityCategorySerializer(category).data}, status=201
        )


class AdminActivityCategoryDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, category_id):
        access = resolve_admin_access(request.user)
        access.require("activity_category.manage")
        with transaction.atomic():
            category = get_object_or_404(
                ActivityCategory.objects.select_for_update(), pk=category_id
            )
            before = activity_category_audit_snapshot(category)
            serializer = AdminActivityCategorySerializer(
                category, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            category = serializer.save()
            action = "activity_category.update"
            if before["is_active"] != category.is_active:
                action = (
                    "activity_category.enable"
                    if category.is_active else "activity_category.disable"
                )
            AdminAuditLog.objects.create(
                actor=request.user,
                organization=access.member.organization if access.member else None,
                action=action,
                target_type="activity_category",
                target_id=str(category.id),
                before=before,
                after=activity_category_audit_snapshot(category),
                request_id=request.headers.get("X-Request-ID", ""),
                ip_address=client_ip(request),
            )
        category = activity_category_admin_queryset().get(pk=category.pk)
        return Response({"data": AdminActivityCategorySerializer(category).data})


class AdminActivityReportListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("activity_report.view")
        query = AdminActivityReportQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = activity_report_admin_queryset(access)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(case_no__icontains=keyword)
                | Q(activity__title__icontains=keyword)
                | Q(reporter__nickname__icontains=keyword)
            )
        if city_code := params.get("city_code", "").strip():
            queryset = queryset.filter(activity__city_code=city_code)
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "pending": summary_queryset.filter(status=ActivityReport.Status.PENDING).count(),
            "processing": summary_queryset.filter(status=ActivityReport.Status.PROCESSING).count(),
            "resolved": summary_queryset.filter(status=ActivityReport.Status.RESOLVED).count(),
        }
        if status_value := params.get("status", ""):
            queryset = queryset.filter(status=status_value)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-created_at", "-id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response({"data": {
            "items": AdminActivityReportSerializer(items, many=True).data,
            "pagination": {"page": page, "page_size": page_size, "total": total},
            "summary": summary,
        }})


class AdminActivityReportActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("activity_report.manage")
        serializer = AdminActivityReportActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = review_activity_report(
            case_no=case_no,
            action=serializer.validated_data["action"],
            result_note=serializer.validated_data.get("result_note", ""),
            actor=request.user,
            access=access,
            request=request,
        )
        return Response({"data": AdminActivityReportSerializer(report).data})


class AdminActivityFinanceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("activity_finance.view")
        query = AdminActivityFinanceQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        payments = ActivityParticipationPaymentOrder.objects.select_related(
            "participation__activity", "payer"
        )
        refunds = ActivityParticipationRefundOrder.objects.select_related(
            "activity", "beneficiary", "payment_order", "operator"
        )
        after_sales = ActivityAfterSalesCase.objects.select_related(
            "participation__activity", "applicant", "reviewed_by", "refund_order"
        )
        settlements = ActivitySettlement.objects.select_related(
            "activity", "beneficiary"
        )
        if not access.all_data:
            payments = payments.filter(
                participation__activity__city_code__in=access.city_codes
            )
            refunds = refunds.filter(activity__city_code__in=access.city_codes)
            after_sales = after_sales.filter(
                participation__activity__city_code__in=access.city_codes
            )
            settlements = settlements.filter(
                activity__city_code__in=access.city_codes
            )
        if city_code := params.get("city_code", "").strip():
            payments = payments.filter(participation__activity__city_code=city_code)
            refunds = refunds.filter(activity__city_code=city_code)
            after_sales = after_sales.filter(
                participation__activity__city_code=city_code
            )
            settlements = settlements.filter(activity__city_code=city_code)
        summary = {
            "paid_count": payments.filter(
                status__in=(
                    ActivityParticipationPaymentOrder.Status.PAID,
                    ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
                    ActivityParticipationPaymentOrder.Status.REFUNDED,
                )
            ).count(),
            "pending_payment_count": payments.filter(
                status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT
            ).count(),
            "refund_count": refunds.count(),
            "refunded_amount": refunds.filter(
                status=ActivityParticipationRefundOrder.Status.SUCCEEDED
            ).aggregate(total=Sum("refund_amount"))["total"] or 0,
            "open_after_sales_count": after_sales.filter(
                status__in=(
                    ActivityAfterSalesCase.Status.PENDING,
                    ActivityAfterSalesCase.Status.PROCESSING,
                )
            ).count(),
            "confirming_settlement_count": settlements.filter(
                status=ActivitySettlement.Status.CONFIRMING
            ).count(),
            "frozen_settlement_count": settlements.filter(
                status=ActivitySettlement.Status.RISK_FROZEN
            ).count(),
            "disputed_settlement_count": settlements.filter(
                status=ActivitySettlement.Status.DISPUTE_FROZEN
            ).count(),
            "settled_count": settlements.filter(
                status=ActivitySettlement.Status.SETTLED
            ).count(),
            "settled_amount": settlements.filter(
                status=ActivitySettlement.Status.SETTLED
            ).aggregate(total=Sum("settlement_amount"))["total"] or 0,
        }
        keyword = params.get("search", "").strip()
        record_type = params["record_type"]
        if record_type == "payment":
            queryset = payments
            if keyword:
                queryset = queryset.filter(
                    Q(order_no__icontains=keyword)
                    | Q(participation__activity__title__icontains=keyword)
                    | Q(payer__nickname__icontains=keyword)
                )
            serializer_class = AdminActivityParticipationPaymentSerializer
        elif record_type == "refund":
            queryset = refunds
            if keyword:
                queryset = queryset.filter(
                    Q(refund_no__icontains=keyword)
                    | Q(payment_order__order_no__icontains=keyword)
                    | Q(activity__title__icontains=keyword)
                    | Q(beneficiary__nickname__icontains=keyword)
                )
            serializer_class = AdminActivityParticipationRefundSerializer
        elif record_type == "after_sales":
            queryset = after_sales
            if keyword:
                queryset = queryset.filter(
                    Q(case_no__icontains=keyword)
                    | Q(participation__activity__title__icontains=keyword)
                    | Q(applicant__nickname__icontains=keyword)
                )
            serializer_class = AdminActivityAfterSalesSerializer
        else:
            queryset = settlements
            if keyword:
                queryset = queryset.filter(
                    Q(settlement_no__icontains=keyword)
                    | Q(activity__title__icontains=keyword)
                    | Q(beneficiary__nickname__icontains=keyword)
                )
            serializer_class = AdminActivitySettlementSerializer
        if status_value := params.get("status", "").strip():
            queryset = queryset.filter(status=status_value)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-created_at", "-id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response({"data": {
            "items": serializer_class(items, many=True).data,
            "pagination": {"page": page, "page_size": page_size, "total": total},
            "summary": summary,
        }})


class AdminActivityAfterSalesActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("activity_after_sales.manage")
        serializer = AdminActivityAfterSalesActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = review_activity_after_sales_case(
            case_no=case_no,
            action=serializer.validated_data["action"],
            result_note=serializer.validated_data.get("result_note", ""),
            approved_principal_amount=serializer.validated_data.get(
                "approved_principal_amount"
            ),
            approved_service_fee_amount=serializer.validated_data.get(
                "approved_service_fee_amount"
            ),
            actor=request.user,
            access=access,
            request=request,
        )
        return Response({"data": AdminActivityAfterSalesSerializer(case).data})


class AdminActivitySettlementActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, settlement_no):
        access = resolve_admin_access(request.user)
        access.require("activity_settlement.manage")
        serializer = AdminActivitySettlementActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        settlement = review_activity_settlement(
            settlement_no=settlement_no,
            action=serializer.validated_data["action"],
            reason=serializer.validated_data.get("reason", ""),
            actor=request.user,
            access=access,
            request=request,
        )
        return Response({"data": AdminActivitySettlementSerializer(settlement).data})


class ProviderApplicationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        query = ProviderApplicationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = scoped_providers(access).select_related("user", "lifestyle_photo").prefetch_related(
            "services__category"
        )
        queryset = queryset.filter(status=params["status"])
        if city_code := params.get("city_code"):
            queryset = queryset.filter(service_city_code=city_code)
        if verification_status := params.get("verification_status"):
            queryset = queryset.filter(user__verification_status=verification_status)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(user__nickname__icontains=keyword) | Q(user__phone__icontains=keyword)
            )
        return paginated_response(
            queryset.order_by("submitted_at", "id"),
            ProviderReviewListSerializer,
            page=params["page"],
            page_size=params["page_size"],
        )


class ProviderApplicationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        profile = get_object_or_404(
            scoped_providers(access).select_related("user", "lifestyle_photo").prefetch_related(
                "services__category"
            ),
            id=profile_id,
        )
        return Response({"data": ProviderReviewListSerializer(profile).data})


class ProviderApplicationReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        serializer = ProviderReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = review_provider_application(
            profile_id=profile_id,
            decision=serializer.validated_data["decision"],
            reason=serializer.validated_data.get("reason", ""),
            actor=request.user,
            access=access,
            request=request,
        )
        profile = ProviderProfile.objects.select_related("user", "lifestyle_photo").prefetch_related(
            "services__category"
        ).get(id=profile.id)
        return Response({"data": ProviderReviewListSerializer(profile).data})


class AdminUserListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("user.view")
        query = AdminUserQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = admin_user_queryset(access)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(nickname__icontains=keyword) | Q(phone__icontains=keyword)
            )
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "verified": summary_queryset.filter(
                verification_status=User.VerificationStatus.VERIFIED
            ).count(),
            "providers": summary_queryset.filter(provider_profile__isnull=False).count(),
            "flagged": summary_queryset.filter(admin_risk_flag__is_active=True).count(),
            "suspended": summary_queryset.filter(
                account_status=User.AccountStatus.SUSPENDED
            ).count(),
        }
        if verification_status := params.get("verification_status"):
            queryset = queryset.filter(verification_status=verification_status)
        if account_status := params.get("account_status"):
            queryset = queryset.filter(account_status=account_status)
        if params["identity"] == "provider":
            queryset = queryset.filter(provider_profile__isnull=False)
        elif params["identity"] == "user":
            queryset = queryset.filter(provider_profile__isnull=True)
        if params["risk"] == "flagged":
            queryset = queryset.filter(admin_risk_flag__is_active=True)
        elif params["risk"] == "unflagged":
            queryset = queryset.exclude(admin_risk_flag__is_active=True)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-date_joined")[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": AdminUserListSerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )


class AdminUserDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, public_id):
        access = resolve_admin_access(request.user)
        access.require("user.view")
        user = get_object_or_404(admin_user_queryset(access), public_id=public_id)
        data = AdminUserListSerializer(user).data
        data["recent_orders"] = [
            {
                "order_no": order.order_no,
                "service_name": order.service_name_snapshot,
                "provider_name": order.provider_name_snapshot,
                "status": order.status,
                "status_label": order.get_status_display(),
                "payable_amount": order.payable_amount,
                "created_at": order.created_at,
            }
            for order in user.provider_orders.select_related("provider").order_by("-created_at")[:5]
        ]
        activities = Activity.objects.filter(
            Q(organizer=user) | Q(participations__user=user)
        ).distinct().order_by("-created_at")[:5]
        data["recent_activities"] = [
            {
                "id": activity.id,
                "title": activity.title,
                "status": activity.status,
                "status_label": activity.get_status_display(),
                "starts_at": activity.starts_at,
            }
            for activity in activities
        ]
        data["addresses"] = [
            {
                "id": address.id,
                "name": address.name,
                "address": address.address,
                "city_name": address.city_name,
                "contact_name": address.contact_name,
                "contact_gender": address.contact_gender,
                "contact_gender_label": address.get_contact_gender_display(),
                "contact_phone": address.contact_phone,
                "longitude": address.longitude,
                "latitude": address.latitude,
                "is_default": address.is_default,
                "updated_at": address.updated_at,
            }
            for address in user.addresses.all()[:20]
        ]
        return Response({"data": data})


class AdminUserAccountActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, public_id):
        access = resolve_admin_access(request.user)
        access.require("user.status.manage")
        serializer = AdminUserAccountActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = change_user_account_status(
            public_id=public_id,
            actor=request.user,
            access=access,
            request=request,
            **serializer.validated_data,
        )
        user = get_object_or_404(admin_user_queryset(access), pk=user.pk)
        return Response({"data": AdminUserListSerializer(user).data})


class AdminUserRiskActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, public_id):
        access = resolve_admin_access(request.user)
        access.require("user.risk.manage")
        serializer = AdminUserRiskActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = change_user_risk_flag(
            public_id=public_id,
            actor=request.user,
            access=access,
            request=request,
            level=serializer.validated_data.get("level", ""),
            action=serializer.validated_data["action"],
            reason=serializer.validated_data["reason"],
        )
        user = get_object_or_404(admin_user_queryset(access), pk=user.pk)
        return Response({"data": AdminUserListSerializer(user).data})


class ProviderAdminListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("provider.view")
        query = ProviderAdminQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = provider_admin_queryset(access)
        if city_code := params.get("city_code"):
            queryset = queryset.filter(service_city_code=city_code)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(user__nickname__icontains=keyword) | Q(user__phone__icontains=keyword)
            )
        summary_queryset = queryset
        online_query = online_provider_query(timezone.now())
        summary = {
            "total": summary_queryset.count(),
            "accepting": summary_queryset.filter(online_query).count(),
            "restricted": summary_queryset.filter(admin_order_restricted=True).count(),
            "suspended": summary_queryset.filter(
                status=ProviderProfile.Status.SUSPENDED
            ).count(),
            "pending": summary_queryset.filter(status=ProviderProfile.Status.PENDING).count(),
        }
        if provider_status := params.get("status"):
            queryset = queryset.filter(status=provider_status)
        if verification_status := params.get("verification_status"):
            queryset = queryset.filter(user__verification_status=verification_status)
        if params["accepting"] == "accepting":
            queryset = queryset.filter(online_query)
        elif params["accepting"] == "paused":
            queryset = queryset.filter(
                status=ProviderProfile.Status.APPROVED,
                admin_order_restricted=False,
            ).exclude(online_query)
        elif params["accepting"] == "restricted":
            queryset = queryset.filter(admin_order_restricted=True)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-updated_at")[(page - 1) * page_size : page * page_size]
        context = {"can_review": can_access(access, "provider.review")}
        return Response(
            {
                "data": {
                    "items": ProviderAdminSerializer(items, many=True, context=context).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )


class ProviderAdminDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.view")
        profile = get_object_or_404(provider_admin_queryset(access), id=profile_id)
        context = {
            "can_review": can_access(access, "provider.review"),
            "include_detail": True,
        }
        data = ProviderAdminSerializer(profile, context=context).data
        data["recent_orders"] = [
            {
                "order_no": order.order_no,
                "customer_name": order.customer.nickname,
                "service_name": order.service_name_snapshot,
                "status": order.status,
                "status_label": order.get_status_display(),
                "payable_amount": order.payable_amount,
                "created_at": order.created_at,
            }
            for order in profile.orders.select_related("customer").order_by("-created_at")[:5]
        ]
        return Response({"data": data})


class ProviderAdminActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.manage")
        serializer = ProviderAdminActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = change_provider_operational_status(
            profile_id=profile_id,
            actor=request.user,
            access=access,
            request=request,
            **serializer.validated_data,
        )
        profile = get_object_or_404(provider_admin_queryset(access), pk=profile.pk)
        context = {
            "can_review": can_access(access, "provider.review"),
            "include_detail": True,
        }
        return Response({"data": ProviderAdminSerializer(profile, context=context).data})


class ProviderCreditAdjustmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.credit.adjust")
        serializer = ProviderCreditAdjustmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = adjust_provider_credit(
            profile_id=profile_id,
            actor=request.user,
            access=access,
            request=request,
            **serializer.validated_data,
        )
        profile = get_object_or_404(provider_admin_queryset(access), pk=profile.pk)
        context = {
            "can_review": can_access(access, "provider.review"),
            "include_detail": True,
        }
        return Response({"data": ProviderAdminSerializer(profile, context=context).data})


class ProviderOrderAdminListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("order.fulfillment.view")
        query = ProviderOrderAdminQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = provider_order_queryset(access)
        if city_code := params.get("city_code"):
            queryset = queryset.filter(provider__service_city_code=city_code)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(order_no__icontains=keyword)
                | Q(customer__nickname__icontains=keyword)
                | Q(customer__phone__icontains=keyword)
                | Q(provider__user__nickname__icontains=keyword)
                | Q(provider__user__phone__icontains=keyword)
                | Q(contact_name__icontains=keyword)
                | Q(contact_phone__icontains=keyword)
            )
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "active": summary_queryset.filter(
                status__in=(ProviderOrder.Status.DEPARTED, ProviderOrder.Status.IN_SERVICE)
            ).count(),
            "pending_confirmation": summary_queryset.filter(
                status=ProviderOrder.Status.PENDING_CONFIRMATION
            ).count(),
            "anomalies": summary_queryset.filter(order_anomaly_query("all")).count(),
        }
        stage_statuses = {
            "active": (ProviderOrder.Status.DEPARTED, ProviderOrder.Status.IN_SERVICE),
            "pending_confirmation": (ProviderOrder.Status.PENDING_CONFIRMATION,),
            "ended": (
                ProviderOrder.Status.PENDING_REVIEW,
                ProviderOrder.Status.COMPLETED,
                ProviderOrder.Status.CANCELLED,
                ProviderOrder.Status.AFTER_SALES,
                ProviderOrder.Status.REFUNDED,
            ),
        }
        if params["stage"] != "all":
            queryset = queryset.filter(status__in=stage_statuses[params["stage"]])
        if order_status := params.get("status"):
            queryset = queryset.filter(status=order_status)
        if params["anomaly"] != "all":
            anomaly_code = "all" if params["anomaly"] == "any" else params["anomaly"]
            queryset = queryset.filter(order_anomaly_query(anomaly_code))
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-created_at")[(page - 1) * page_size : page * page_size]
        return Response(
            {
                "data": {
                    "items": ProviderOrderAdminSerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )


class ProviderOrderAdminDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_no):
        access = resolve_admin_access(request.user)
        access.require("order.fulfillment.view")
        order = get_admin_provider_order(access, order_no)
        return Response({"data": ProviderOrderAdminSerializer(order).data})


class ProviderOrderEvidenceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, order_no):
        access = resolve_admin_access(request.user)
        access.require("order.fulfillment.view")
        order = get_admin_provider_order(access, order_no)
        if not order.arrival_photo_id:
            return Response({"detail": "该订单没有集合地点照片。"}, status=404)
        organization = access.member.organization if access.member else None
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=organization,
            action="order.fulfillment.evidence.view",
            target_type="provider_order",
            target_id=order.order_no,
            after={"media_asset_id": str(order.arrival_photo_id)},
            request_id=request.headers.get("X-Request-ID", ""),
            ip_address=client_ip(request),
        )
        return Response(
            {
                "data": {
                    "url": build_media_url(order.arrival_photo.object_key, private=True),
                    "expires_in": settings.COS_SIGNED_PRIVATE_URL_TTL,
                }
            }
        )


class ProviderOrderSupportNoteView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, order_no):
        access = resolve_admin_access(request.user)
        access.require("order.support_note.add")
        order = get_admin_provider_order(access, order_no)
        serializer = ProviderOrderSupportNoteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = access.member.organization if access.member else None
        note = ProviderOrderSupportNote.objects.create(
            order=order,
            author=request.user,
            organization=organization,
            content=serializer.validated_data["content"],
        )
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=organization,
            action="order.support_note.add",
            target_type="provider_order",
            target_id=order.order_no,
            after={"note_id": note.id, "content": note.content},
            request_id=request.headers.get("X-Request-ID", ""),
            ip_address=client_ip(request),
        )
        return Response(
            {"data": ProviderOrderSupportNoteSerializer(note).data}, status=201
        )


class ProviderOrderAfterSalesListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("order.after_sales.view")
        query = ProviderOrderAfterSalesCaseQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = provider_order_after_sales_queryset(access)
        if city_code := params.get("city_code"):
            queryset = queryset.filter(order__provider__service_city_code=city_code)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(case_no__icontains=keyword)
                | Q(order__order_no__icontains=keyword)
                | Q(order__customer__nickname__icontains=keyword)
                | Q(order__customer__phone__icontains=keyword)
                | Q(order__provider__user__nickname__icontains=keyword)
                | Q(order__provider__user__phone__icontains=keyword)
            )
        summary_queryset = queryset
        summary = {
            "total": summary_queryset.count(),
            "pending": summary_queryset.filter(
                status=ProviderOrderAfterSalesCase.Status.PENDING
            ).count(),
            "processing": summary_queryset.filter(
                status=ProviderOrderAfterSalesCase.Status.PROCESSING
            ).count(),
            "approved": summary_queryset.filter(
                status=ProviderOrderAfterSalesCase.Status.APPROVED
            ).count(),
        }
        if case_status := params.get("status"):
            queryset = queryset.filter(status=case_status)
        if case_type := params.get("case_type"):
            queryset = queryset.filter(case_type=case_type)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset.order_by("-created_at", "-id")[
            (page - 1) * page_size : page * page_size
        ]
        return Response(
            {
                "data": {
                    "items": ProviderOrderAfterSalesCaseSerializer(items, many=True).data,
                    "pagination": {"page": page, "page_size": page_size, "total": total},
                    "summary": summary,
                }
            }
        )

    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("order.after_sales.review")
        serializer = ProviderOrderAfterSalesCaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = create_provider_order_after_sales_case(
            actor=request.user,
            access=access,
            request=request,
            **serializer.validated_data,
        )
        case = get_object_or_404(provider_order_after_sales_queryset(access), pk=case.pk)
        return Response(
            {"data": ProviderOrderAfterSalesCaseSerializer(case).data}, status=201
        )


class ProviderOrderAfterSalesDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("order.after_sales.view")
        case = get_object_or_404(provider_order_after_sales_queryset(access), case_no=case_no)
        return Response({"data": ProviderOrderAfterSalesCaseSerializer(case).data})


class ProviderOrderAfterSalesActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, case_no):
        access = resolve_admin_access(request.user)
        access.require("order.after_sales.review")
        serializer = ProviderOrderAfterSalesCaseActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        case = review_provider_order_after_sales_case(
            case_no=case_no,
            actor=request.user,
            access=access,
            request=request,
            approved_amount=serializer.validated_data.get("approved_amount"),
            result_note=serializer.validated_data.get("result_note", ""),
            action=serializer.validated_data["action"],
        )
        case = get_object_or_404(provider_order_after_sales_queryset(access), pk=case.pk)
        return Response({"data": ProviderOrderAfterSalesCaseSerializer(case).data})


class OrganizationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        queryset = Organization.objects.all() if access.all_data else Organization.objects.filter(
            id=access.member.organization_id
        )
        return Response({"data": {"items": AdminOrganizationSerializer(queryset, many=True).data}})


class ProviderOrderingSettingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        return Response({"data": ProviderOrderingSettingSerializer(ProviderOrderingSetting.current()).data})

    @transaction.atomic
    def patch(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        setting = ProviderOrderingSetting.current()
        before = ProviderOrderingSettingSerializer(setting).data
        serializer = ProviderOrderingSettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AdminAuditLog.objects.create(
            actor=request.user, organization=access.member.organization,
            action="operations.provider_ordering.update", target_type="operation_setting",
            target_id="provider-ordering", before=before, after=serializer.data,
            ip_address=client_ip(request),
        )
        return Response({"data": serializer.data})


class OrganizationMemberListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        queryset = OrganizationMember.objects.select_related("user", "organization", "role")
        if not access.all_data:
            queryset = queryset.filter(organization=access.member.organization)
        return Response({"data": {"items": OrganizationMemberSerializer(queryset, many=True).data}})


class AuditLogListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("audit.view")
        queryset = AdminAuditLog.objects.select_related("actor", "organization")
        if not access.all_data:
            queryset = queryset.filter(organization=access.member.organization)
        keyword = request.query_params.get("search", "").strip()
        action = request.query_params.get("action", "").strip()
        target_type = request.query_params.get("target_type", "").strip()
        if keyword:
            queryset = queryset.filter(
                Q(action__icontains=keyword)
                | Q(target_id__icontains=keyword)
                | Q(actor__nickname__icontains=keyword)
            )
        if action:
            queryset = queryset.filter(action__startswith=action)
        if target_type:
            queryset = queryset.filter(target_type=target_type)
        total = queryset.count()
        page = max(int(request.query_params.get("page", 1) or 1), 1)
        page_size = min(max(int(request.query_params.get("page_size", 20) or 20), 1), 100)
        start = (page - 1) * page_size
        items = queryset[start : start + page_size]
        return Response({"data": {"items": AuditLogSerializer(items, many=True).data, "pagination": {"page": page, "page_size": page_size, "total": total}}})
