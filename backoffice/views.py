from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Count, Prefetch, Q, Sum
from django.db.models.functions import Cast, TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
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
from engagements.models import BrowsingHistory
from mediafiles.services import build_media_url
from orders.models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderReview,
    ProviderOrderSettlement,
)
from providers.models import (
    ProviderProfile,
    ProviderProfileRevision,
    ProviderService,
    ProviderServiceRevision,
    ServiceCategory,
)
from providers.presence import online_provider_query
from taskcenter.models import ScheduledTask
from taskcenter.serializers import ScheduledTaskQuerySerializer, ScheduledTaskSerializer
from taskcenter.services import (
    TASK_LEASE_TIMEOUT,
    cancel_provider_rejection_support_timeout,
    retry_failed_task,
)

from .access import client_ip, resolve_admin_access
from .models import (
    AdminAuditLog,
    AdminRole,
    Organization,
    OrganizationMember,
    ProviderOrderAfterSalesCase,
    ProviderOrderSupportNote,
    ProviderOrderingSetting,
    PlatformOperationSetting,
)
from .operation_settings import platform_operation_rules
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
    AdminRoleMutationSerializer,
    AdminRoleSerializer,
    AuditLogQuerySerializer,
    AuditLogSerializer,
    OrganizationMemberSerializer,
    OrganizationMemberCreateSerializer,
    OrganizationMemberUpdateSerializer,
    ProviderAdminActionSerializer,
    ProviderAdminQuerySerializer,
    ProviderAdminSerializer,
    ProviderCreditAdjustmentInputSerializer,
    ProviderOrderAfterSalesCaseActionSerializer,
    ProviderOrderAfterSalesCaseCreateSerializer,
    ProviderOrderAfterSalesCaseQuerySerializer,
    ProviderOrderAfterSalesCaseSerializer,
    ProviderOrderFinanceQuerySerializer,
    ProviderOrderPaymentOrderSerializer,
    ProviderOrderRefundOrderSerializer,
    ProviderOrderSettlementSerializer,
    ProviderOrderAdminQuerySerializer,
    ProviderOrderAdminSerializer,
    ProviderOrderReviewActionSerializer,
    ProviderOrderSupportNoteInputSerializer,
    ProviderOrderSupportNoteSerializer,
    ProviderOrderingSettingSerializer,
    PlatformOperationSettingSerializer,
    ProviderApplicationQuerySerializer,
    ProviderApplicationReviewDecisionSerializer,
    ProviderChangeReviewQuerySerializer,
    ProviderReviewDecisionSerializer,
    ProviderReviewListSerializer,
)
from .permission_catalog import permission_catalog_data
from .services import (
    adjust_provider_credit,
    change_provider_operational_status,
    change_user_account_status,
    change_user_risk_flag,
    create_provider_order_after_sales_case,
    moderate_provider_order_review,
    review_provider_order_after_sales_case,
    review_provider_application,
    review_provider_onboarding,
    review_provider_profile_revision,
    review_provider_service_revision,
    review_provider_identity,
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
    browsing_filter = Q()
    review_filter = Q()
    if not access.all_data:
        browsing_filter = (
            Q(browsing_history__provider__service_city_code__in=access.city_codes)
            | Q(browsing_history__activity__city_code__in=access.city_codes)
        )
        review_filter = Q(
            provider_order_reviews__provider__service_city_code__in=access.city_codes
        )
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
            browsing_count=Count(
                "browsing_history", filter=browsing_filter, distinct=True
            ),
            review_count=Count(
                "provider_order_reviews", filter=review_filter, distinct=True
            ),
        )
    )


def provider_admin_queryset(access):
    return (
        scoped_providers(access)
        .select_related(
            "user",
            "lifestyle_photo",
            "live_location",
            "identity_front_photo",
            "identity_back_photo",
            "identity_face_photo",
        )
        .prefetch_related(
            "services__category",
            "weekly_availability",
            "admin_credit_adjustments__operator",
            "admin_credit_adjustments__organization",
        )
    )


def can_access(access, permission):
    return "*" in access.permissions or permission in access.permissions


def scoped_organizations(access):
    queryset = Organization.objects.all()
    if not access.all_data:
        queryset = queryset.filter(pk=access.member.organization_id)
    return queryset


def _record_refund_retry_audit(
    *, request, access, action, target_type, target_id, before, after
):
    AdminAuditLog.objects.create(
        actor=request.user,
        organization=access.member.organization if access.member else None,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )


def _provider_refund_retry_failed_response(
    *, request, access, refund, action, before, exception
):
    refund.refresh_from_db()
    if refund.status != ProviderOrderRefundOrder.Status.FAILED:
        raise exception
    _record_refund_retry_audit(
        request=request,
        access=access,
        action=action,
        target_type="provider_order_refund",
        target_id=refund.refund_no,
        before=before,
        after={
            "status": refund.status,
            "failure_reason": refund.failure_reason,
        },
    )
    raise ValidationError(
        {"refund": f"退款渠道处理失败：{refund.failure_reason or '请稍后重试。'}"}
    ) from exception


def scoped_admin_roles(access):
    queryset = AdminRole.objects.select_related("organization")
    if not access.all_data:
        queryset = queryset.filter(
            Q(organization=access.member.organization) | Q(organization__isnull=True)
        )
    return queryset


def scoped_organization_members(access):
    queryset = OrganizationMember.objects.select_related(
        "user", "organization", "role"
    )
    if not access.all_data:
        queryset = queryset.filter(organization=access.member.organization)
    return queryset


def role_has_permission(role, permission):
    return "*" in role.permissions or permission in role.permissions


def ensure_organization_manager_remains(member, next_role=None, next_active=None):
    next_role = next_role or member.role
    next_active = member.is_active if next_active is None else next_active
    currently_manages = member.is_active and role_has_permission(
        member.role, "organization.manage"
    )
    will_manage = next_active and role_has_permission(next_role, "organization.manage")
    if not currently_manages or will_manage:
        return
    other_managers = OrganizationMember.objects.filter(
        organization=member.organization,
        is_active=True,
    ).exclude(pk=member.pk).select_related("role")
    if not any(
        role_has_permission(other.role, "organization.manage")
        for other in other_managers
    ):
        raise ValidationError("当前成员是该组织最后一名账号管理员，不能移除其管理权限。")


def ensure_role_manager_remains(role, next_permissions):
    if not role_has_permission(role, "organization.manage"):
        return
    if "organization.manage" in next_permissions or "*" in next_permissions:
        return
    members_using_role = OrganizationMember.objects.filter(
        role=role,
        is_active=True,
    )
    if not members_using_role.exists():
        return
    other_managers = OrganizationMember.objects.filter(
        organization=role.organization,
        is_active=True,
    ).exclude(role=role).select_related("role")
    if not any(
        role_has_permission(other.role, "organization.manage")
        for other in other_managers
    ):
        raise ValidationError("该角色承载当前组织最后一组账号管理权限，不能移除。")


def role_audit_snapshot(role):
    return {
        "organization": role.organization_id,
        "name": role.name,
        "code": role.code,
        "permissions": role.permissions,
        "data_scope": role.data_scope,
        "is_system": role.is_system,
    }


def member_audit_snapshot(member):
    return {
        "user": member.user_id,
        "phone": member.user.phone,
        "organization": member.organization_id,
        "role": member.role_id,
        "role_name": member.role.name,
        "city_codes": member.city_codes,
        "is_active": member.is_active,
    }


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
        "platform_commission_rate": str(category.platform_commission_rate),
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
            "tags",
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
        activity_count=Count("tagged_activities", distinct=True),
        active_activity_count=Count(
            "tagged_activities",
            filter=Q(
                tagged_activities__status__in=(
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
            auto_confirmed_at__isnull=True,
        )
    )
    confirmation_overdue = Q(
        status=ProviderOrder.Status.PENDING_CONFIRMATION,
        confirmation_expires_at__lte=timezone.now(),
    ) | Q(
        status=ProviderOrder.Status.PENDING_CONFIRMATION,
        confirmation_expires_at__isnull=True,
        completion_submitted_at__lte=timezone.now()
        - timedelta(days=platform_operation_rules()["provider_order_confirmation_timeout_days"]),
    )
    support_contact_overdue = Q(
        status=ProviderOrder.Status.PENDING_SUPPORT,
        provider_rejected_at__isnull=False,
        support_contact_deadline_at__lte=timezone.now(),
        support_contacted_at__isnull=True,
    )
    mapping = {
        "missing_evidence": missing_evidence,
        "timeline_gap": timeline_gap,
        "confirmation_overdue": confirmation_overdue,
        "support_contact_overdue": support_contact_overdue,
    }
    if code == "all":
        return missing_evidence | timeline_gap | confirmation_overdue | support_contact_overdue
    return mapping[code]


def provider_order_queryset(access):
    return scoped_provider_orders(access).select_related(
        "customer", "provider__user", "service__category", "arrival_photo",
        "review__customer", "review__audited_by",
        "payment_order",
        "settlement",
        "support_contacted_by",
    ).prefetch_related(
        "review__images",
        "support_notes__author",
        "support_notes__organization",
        "after_sales_cases__creator",
        "after_sales_cases__organization",
        "after_sales_cases__reviewed_by",
        "refund_orders",
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
    ).prefetch_related("order__refund_orders__payment_order", "order__refund_orders__operator")
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


def provider_review_summary(access):
    providers = scoped_providers(access)
    counts = {
        "applications": providers.filter(status=ProviderProfile.Status.PENDING).count(),
        "onboarding": providers.filter(
            onboarding_status=ProviderProfile.OnboardingStatus.PENDING_REVIEW
        ).count(),
        "profile_changes": ProviderProfileRevision.objects.filter(
            provider__in=providers,
            provider__onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            status=ProviderProfileRevision.Status.PENDING,
        ).count(),
        "service_changes": ProviderServiceRevision.objects.filter(
            provider__in=providers,
            provider__onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            status=ProviderServiceRevision.Status.PENDING,
        ).count(),
    }
    return {**counts, "total": sum(counts.values())}


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
        today = timezone.localdate()
        orders = ProviderOrder.objects.all()
        activities = Activity.objects.all()
        if not access.all_data:
            orders = orders.filter(provider__service_city_code__in=access.city_codes)
            activities = activities.filter(city_code__in=access.city_codes)
        trend = build_order_trend(orders, days=days)
        pending_provider_reviews = provider_review_summary(access)["total"]
        week_transaction_amount = sum(
            point["transaction_amount"] for point in trend["points"][-7:]
        )
        return Response(
            {
                "data": {
                    "metrics": {
                        "today_new_users": User.objects.filter(date_joined__date=today).count()
                        if access.all_data else None,
                        "pending_providers": pending_provider_reviews,
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
                            "count": pending_provider_reviews,
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
            queryset = queryset.filter(tags__slug=category).distinct()

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
                tags__in=summary_queryset,
                status__in=(
                    Activity.Status.PENDING_REVIEW,
                    Activity.Status.RECRUITING,
                    Activity.Status.FORMED,
                    Activity.Status.IN_PROGRESS,
                ),
            ).distinct().count(),
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


class ProviderOrderFinanceListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("order.finance.view")
        query = ProviderOrderFinanceQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data

        payments = ProviderOrderPaymentOrder.objects.select_related(
            "order__provider", "payer"
        )
        refunds = ProviderOrderRefundOrder.objects.select_related(
            "order__provider", "payment_order", "beneficiary", "operator"
        )
        settlements = ProviderOrderSettlement.objects.select_related(
            "order", "provider"
        )
        if not access.all_data:
            payments = payments.filter(order__provider__service_city_code__in=access.city_codes)
            refunds = refunds.filter(order__provider__service_city_code__in=access.city_codes)
            settlements = settlements.filter(provider__service_city_code__in=access.city_codes)
        if city_code := params.get("city_code", "").strip():
            payments = payments.filter(order__provider__service_city_code=city_code)
            refunds = refunds.filter(order__provider__service_city_code=city_code)
            settlements = settlements.filter(provider__service_city_code=city_code)

        summary = {
            "paid_amount": payments.filter(
                status__in=(
                    ProviderOrderPaymentOrder.Status.PAID,
                    ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
                    ProviderOrderPaymentOrder.Status.REFUNDED,
                )
            ).aggregate(total=Sum("payable_amount"))["total"] or 0,
            "refunded_amount": refunds.filter(
                status=ProviderOrderRefundOrder.Status.SUCCEEDED
            ).aggregate(total=Sum("refund_amount"))["total"] or 0,
            "pending_settlement_amount": settlements.filter(
                status__in=(
                    ProviderOrderSettlement.Status.RISK_FROZEN,
                    ProviderOrderSettlement.Status.DISPUTE_FROZEN,
                )
            ).aggregate(total=Sum("provider_settlement_amount"))["total"] or 0,
            "settled_amount": settlements.filter(
                status=ProviderOrderSettlement.Status.SETTLED
            ).aggregate(total=Sum("provider_settlement_amount"))["total"] or 0,
            "exception_count": refunds.filter(
                status=ProviderOrderRefundOrder.Status.FAILED
            ).count(),
        }

        record_type = params["record_type"]
        search = params.get("search", "").strip()
        status_value = params.get("status", "").strip()
        if record_type == "payment":
            queryset = payments
            serializer_class = ProviderOrderPaymentOrderSerializer
            if search:
                queryset = queryset.filter(
                    Q(payment_no__icontains=search)
                    | Q(order__order_no__icontains=search)
                    | Q(payer__nickname__icontains=search)
                    | Q(order__provider_name_snapshot__icontains=search)
                )
        elif record_type in ("refund", "exception"):
            queryset = refunds
            serializer_class = ProviderOrderRefundOrderSerializer
            if record_type == "exception":
                queryset = queryset.filter(status=ProviderOrderRefundOrder.Status.FAILED)
            if search:
                queryset = queryset.filter(
                    Q(refund_no__icontains=search)
                    | Q(order__order_no__icontains=search)
                    | Q(beneficiary__nickname__icontains=search)
                    | Q(order__provider_name_snapshot__icontains=search)
                )
        else:
            queryset = settlements
            serializer_class = ProviderOrderSettlementSerializer
            if search:
                queryset = queryset.filter(
                    Q(settlement_no__icontains=search)
                    | Q(order__order_no__icontains=search)
                    | Q(order__provider_name_snapshot__icontains=search)
                )
        if status_value and record_type != "exception":
            queryset = queryset.filter(status=status_value)
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset[(page - 1) * page_size : page * page_size]
        return Response({"data": {
            "items": serializer_class(items, many=True).data,
            "pagination": {"page": page, "page_size": page_size, "total": total},
            "summary": summary,
        }})


class ProviderOrderRefundRetryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, refund_no):
        access = resolve_admin_access(request.user)
        access.require("order.finance.manage")
        queryset = ProviderOrderRefundOrder.objects.select_related("order__provider")
        if not access.all_data:
            queryset = queryset.filter(
                order__provider__service_city_code__in=access.city_codes
            )
        refund = get_object_or_404(queryset, refund_no=refund_no)
        from orders.services import (
            process_provider_order_refund,
            provider_order_refund_can_retry,
        )

        if not provider_order_refund_can_retry(refund):
            raise ValidationError("当前退款单不需要重试。")

        before = {"status": refund.status, "failure_reason": refund.failure_reason}
        try:
            process_provider_order_refund(refund.refund_no)
        except Exception as exc:
            _provider_refund_retry_failed_response(
                request=request,
                access=access,
                refund=refund,
                action="order.finance.refund.retry",
                before=before,
                exception=exc,
            )
        refund = ProviderOrderRefundOrder.objects.select_related(
            "order__provider", "payment_order", "beneficiary", "operator"
        ).get(pk=refund.pk)
        _record_refund_retry_audit(
            request=request,
            access=access,
            action="order.finance.refund.retry",
            target_type="provider_order_refund",
            target_id=refund.refund_no,
            before=before,
            after={
                "status": refund.status,
                "failure_reason": refund.failure_reason,
            },
        )
        return Response({"data": ProviderOrderRefundOrderSerializer(refund).data})


class ActivityParticipationRefundRetryView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, refund_no):
        access = resolve_admin_access(request.user)
        access.require("activity_after_sales.manage")
        queryset = ActivityParticipationRefundOrder.objects.select_related("activity")
        if not access.all_data:
            queryset = queryset.filter(activity__city_code__in=access.city_codes)
        refund = get_object_or_404(queryset, refund_no=refund_no)
        from activities.services import (
            activity_participation_refund_can_retry,
            process_activity_participation_refund,
        )

        if not activity_participation_refund_can_retry(refund):
            raise ValidationError("当前活动退款单不需要重试。")

        before = {"status": refund.status, "failure_reason": refund.failure_reason}
        try:
            process_activity_participation_refund(refund.refund_no)
        except Exception as exc:
            refund.refresh_from_db()
            if refund.status != ActivityParticipationRefundOrder.Status.FAILED:
                raise
            _record_refund_retry_audit(
                request=request,
                access=access,
                action="activity.finance.refund.retry",
                target_type="activity_participation_refund",
                target_id=refund.refund_no,
                before=before,
                after={
                    "status": refund.status,
                    "failure_reason": refund.failure_reason,
                },
            )
            raise ValidationError(
                {
                    "refund": (
                        "退款渠道处理失败："
                        f"{refund.failure_reason or '请稍后重试。'}"
                    )
                }
            ) from exc
        refund = ActivityParticipationRefundOrder.objects.select_related(
            "activity", "beneficiary", "payment_order", "operator"
        ).get(pk=refund.pk)
        _record_refund_retry_audit(
            request=request,
            access=access,
            action="activity.finance.refund.retry",
            target_type="activity_participation_refund",
            target_id=refund.refund_no,
            before=before,
            after={
                "status": refund.status,
                "failure_reason": refund.failure_reason,
            },
        )
        return Response(
            {"data": AdminActivityParticipationRefundSerializer(refund).data}
        )


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
            "services__category", "category_grants__category"
        )
        queryset = queryset.filter(status=params["status"])
        if city_code := params.get("city_code"):
            queryset = queryset.filter(service_city_code=city_code)
        if keyword := params.get("search", "").strip():
            queryset = queryset.filter(
                Q(user__nickname__icontains=keyword)
                | Q(user__phone__icontains=keyword)
                | Q(application_real_name__icontains=keyword)
            )
        return paginated_response(
            queryset.order_by("submitted_at", "id"),
            ProviderReviewListSerializer,
            page=params["page"],
            page_size=params["page_size"],
        )


class ProviderReviewSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        return Response({"data": provider_review_summary(access)})


class ProviderApplicationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        profile = get_object_or_404(
            scoped_providers(access).select_related("user", "lifestyle_photo").prefetch_related(
                "services__category", "category_grants__category"
            ),
            id=profile_id,
        )
        return Response({"data": ProviderReviewListSerializer(profile).data})


class ProviderApplicationReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        serializer = ProviderApplicationReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = review_provider_application(
            profile_id=profile_id,
            decision=serializer.validated_data["decision"],
            reason=serializer.validated_data.get("reason", ""),
            allowed_category_ids=serializer.validated_data.get("allowed_category_ids", []),
            actor=request.user,
            access=access,
            request=request,
        )
        profile = ProviderProfile.objects.select_related("user", "lifestyle_photo").prefetch_related(
            "services__category", "category_grants__category"
        ).get(id=profile.id)
        return Response({"data": ProviderReviewListSerializer(profile).data})


def _profile_revision_payload(revision):
    if not revision:
        return None
    return {
        "id": revision.id,
        "display_name": revision.display_name,
        "bio": revision.bio,
        "lifestyle_photo_url": build_media_url(revision.lifestyle_photo.object_key),
        "service_city_code": revision.service_city_code,
        "service_city_name": revision.service_city_name,
        "max_service_radius_km": revision.max_service_radius_km,
        "status": revision.status,
        "rejection_reason": revision.rejection_reason,
        "submitted_at": revision.submitted_at,
    }


def _service_revision_payload(revision):
    minimum, maximum = revision.category.price_range_for(revision.billing_type)
    return {
        "id": revision.id,
        "service_id": revision.service_id,
        "action": revision.action,
        "action_label": revision.get_action_display(),
        "category_id": revision.category_id,
        "category_name": revision.category.name,
        "billing_type": revision.billing_type,
        "billing_type_label": revision.get_billing_type_display(),
        "price_amount": revision.price_amount,
        "min_price_amount": minimum,
        "max_price_amount": maximum,
        "estimated_duration_minutes": revision.estimated_duration_minutes,
        "description": revision.description,
        "status": revision.status,
        "rejection_reason": revision.rejection_reason,
        "submitted_at": revision.submitted_at,
    }


def _provider_change_payload(kind, obj):
    provider = obj if kind == "onboarding" else obj.provider
    base = {
        "id": provider.id if kind == "onboarding" else obj.id,
        "kind": kind,
        "provider_id": provider.id,
        "provider_name": provider.public_display_name,
        "application_real_name": provider.application_real_name,
        "identity_real_name": provider.identity_real_name,
        "identity_name_matches": (
            not provider.application_real_name
            or provider.application_real_name.strip() == provider.identity_real_name.strip()
        ),
        "phone": provider.user.phone,
        "service_city_name": provider.service_city_name,
    }
    if kind == "onboarding":
        profile_revision = provider.profile_revisions.filter(
            status=ProviderProfileRevision.Status.PENDING
        ).select_related("lifestyle_photo").first()
        services = provider.service_revisions.filter(
            status=ProviderServiceRevision.Status.PENDING
        ).select_related("category", "service")
        base.update({
            "status": provider.onboarding_status,
            "submitted_at": provider.onboarding_submitted_at,
            "rejection_reason": provider.onboarding_rejection_reason,
            "identity": {
                "status": provider.identity_status,
                "number_masked": provider.identity_number_masked,
                "front_photo_url": build_media_url(
                    provider.identity_front_photo.object_key, private=True
                ) if provider.identity_front_photo_id else None,
                "back_photo_url": build_media_url(
                    provider.identity_back_photo.object_key, private=True
                ) if provider.identity_back_photo_id else None,
                "face_photo_url": build_media_url(
                    provider.identity_face_photo.object_key, private=True
                ) if provider.identity_face_photo_id else None,
            },
            "profile_revision": _profile_revision_payload(profile_revision),
            "service_revisions": [_service_revision_payload(item) for item in services],
        })
    elif kind == "profile":
        base.update({
            "status": obj.status,
            "submitted_at": obj.submitted_at,
            "rejection_reason": obj.rejection_reason,
            "profile_revision": _profile_revision_payload(obj),
            "service_revisions": [],
        })
    else:
        base.update({
            "status": obj.status,
            "submitted_at": obj.submitted_at,
            "rejection_reason": obj.rejection_reason,
            "profile_revision": None,
            "service_revisions": [_service_revision_payload(obj)],
        })
    return base


class ProviderChangeReviewListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        query = ProviderChangeReviewQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        kind = params["kind"]
        requested_status = params["status"]
        if kind == "onboarding":
            status_value = {
                "pending": ProviderProfile.OnboardingStatus.PENDING_REVIEW,
                "approved": ProviderProfile.OnboardingStatus.APPROVED,
                "rejected": ProviderProfile.OnboardingStatus.REJECTED,
            }[requested_status]
            queryset = scoped_providers(access).filter(onboarding_status=status_value).select_related(
                "user", "identity_front_photo", "identity_back_photo", "identity_face_photo"
            ).order_by("-onboarding_submitted_at", "-id")
        elif kind == "profile":
            queryset = ProviderProfileRevision.objects.filter(
                status=requested_status,
                provider__onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            ).select_related("provider__user", "lifestyle_photo")
            if not access.all_data:
                queryset = queryset.filter(provider__service_city_code__in=access.city_codes)
            queryset = queryset.order_by("-submitted_at", "-id")
        else:
            queryset = ProviderServiceRevision.objects.filter(
                status=requested_status,
                provider__onboarding_status=ProviderProfile.OnboardingStatus.APPROVED,
            ).select_related("provider__user", "category", "service")
            if not access.all_data:
                queryset = queryset.filter(provider__service_city_code__in=access.city_codes)
            queryset = queryset.order_by("-submitted_at", "-id")
        page = params["page"]
        page_size = params["page_size"]
        total = queryset.count()
        items = queryset[(page - 1) * page_size : page * page_size]
        return Response({"data": {
            "items": [_provider_change_payload(kind, item) for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }})


class ProviderChangeReviewActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, kind, review_id):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        serializer = ProviderReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        kwargs = {
            "decision": serializer.validated_data["decision"],
            "reason": serializer.validated_data.get("reason", ""),
            "actor": request.user,
            "access": access,
            "request": request,
        }
        if kind == "onboarding":
            result = review_provider_onboarding(profile_id=review_id, **kwargs)
            result_status = result.onboarding_status
        elif kind == "profile":
            result = review_provider_profile_revision(revision_id=review_id, **kwargs)
            result_status = result.status
        elif kind == "service":
            result = review_provider_service_revision(revision_id=review_id, **kwargs)
            result_status = result.status
        else:
            raise ValidationError({"kind": "不支持的审核类型。"})
        return Response({"data": {"id": review_id, "kind": kind, "status": result_status}})


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
            "providers": summary_queryset.filter(provider_profile__isnull=False).count(),
            "flagged": summary_queryset.filter(admin_risk_flag__is_active=True).count(),
            "suspended": summary_queryset.filter(
                account_status=User.AccountStatus.SUSPENDED
            ).count(),
        }
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
        browsing_history = BrowsingHistory.objects.filter(user=user).select_related(
            "provider__user", "activity"
        )
        reviews = ProviderOrderReview.objects.filter(customer=user).select_related(
            "order", "provider__user"
        )
        if not access.all_data:
            browsing_history = browsing_history.filter(
                Q(provider__service_city_code__in=access.city_codes)
                | Q(activity__city_code__in=access.city_codes)
            )
            reviews = reviews.filter(provider__service_city_code__in=access.city_codes)
        data["browsing_history"] = [
            {
                "id": item.id,
                "target_type": item.target_type,
                "target_type_label": item.get_target_type_display(),
                "target_id": (
                    str(item.provider.user.public_id)
                    if item.provider_id
                    else str(item.activity_id)
                ),
                "title": (
                    item.provider.public_display_name
                    if item.provider_id
                    else item.activity.title
                ),
                "city_name": (
                    item.provider.service_city_name
                    if item.provider_id
                    else item.activity.city_name
                ),
                "view_count": item.view_count,
                "first_viewed_at": item.created_at,
                "last_viewed_at": item.viewed_at,
            }
            for item in browsing_history.order_by("-viewed_at")[:50]
        ]
        data["reviews"] = [
            {
                "id": item.id,
                "order_no": item.order.order_no,
                "provider_name": item.provider.public_display_name,
                "service_name": item.order.service_name_snapshot,
                "rating": item.rating,
                "content": item.content,
                "audit_status": item.audit_status,
                "audit_status_label": item.get_audit_status_display(),
                "is_visible": item.is_visible,
                "created_at": item.created_at,
            }
            for item in reviews.order_by("-created_at")[:50]
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
                Q(display_name__icontains=keyword)
                | Q(user__nickname__icontains=keyword)
                | Q(user__phone__icontains=keyword)
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
        if identity_status := params.get("identity_status"):
            queryset = queryset.filter(identity_status=identity_status)
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


class ProviderIdentityReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, profile_id):
        access = resolve_admin_access(request.user)
        access.require("provider.review")
        serializer = ProviderReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        profile = review_provider_identity(
            profile_id=profile_id,
            decision=serializer.validated_data["decision"],
            reason=serializer.validated_data.get("reason", ""),
            actor=request.user,
            access=access,
            request=request,
        )
        context = {"can_review": True, "include_detail": True}
        return Response({"data": ProviderAdminSerializer(profile, context=context).data})


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
            "pending_reviews": summary_queryset.filter(
                review__audit_status=ProviderOrderReview.AuditStatus.PENDING
            ).count(),
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
        if params["review_audit_status"] != "all":
            queryset = queryset.filter(review__audit_status=params["review_audit_status"])
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


class ProviderOrderReviewActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, order_no):
        access = resolve_admin_access(request.user)
        access.require("order.review.manage")
        serializer = ProviderOrderReviewActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        moderate_provider_order_review(
            order_no=order_no,
            actor=request.user,
            access=access,
            request=request,
            **serializer.validated_data,
        )
        return Response(
            {"data": ProviderOrderAdminSerializer(get_admin_provider_order(access, order_no)).data}
        )


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
        serializer = ProviderOrderSupportNoteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(
            provider_order_queryset(access).select_for_update(of=("self",)),
            order_no=order_no,
        )
        marks_customer_contact = serializer.validated_data["marks_customer_contact"]
        contact_marked = marks_customer_contact and not order.support_contacted_at
        if contact_marked:
            if (
                order.status != ProviderOrder.Status.PENDING_SUPPORT
                or not order.provider_rejected_at
                or not order.support_contact_deadline_at
            ):
                raise ValidationError({
                    "marks_customer_contact": "只有达人主动拒单后的待客服订单可以登记有效联系。"
                })
            if order.support_contact_deadline_at <= timezone.now():
                raise ValidationError({
                    "marks_customer_contact": "客服有效联系截止时间已到，不能再阻止自动退款。"
                })
            refund_reference = f"provider-rejection-timeout:{order.order_no}"
            if order.refund_orders.filter(idempotency_key=refund_reference).exists():
                raise ValidationError({
                    "marks_customer_contact": "系统已经发起自动退款，不能再登记为已联系。"
                })
        organization = access.member.organization if access.member else None
        note = ProviderOrderSupportNote.objects.create(
            order=order,
            author=request.user,
            organization=organization,
            content=serializer.validated_data["content"],
        )
        if contact_marked:
            order.support_contacted_at = timezone.now()
            order.support_contacted_by = request.user
            order.save(update_fields=("support_contacted_at", "support_contacted_by", "updated_at"))
            cancel_provider_rejection_support_timeout(order.order_no, "customer_contacted")
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=organization,
            action=(
                "order.support_contact.mark"
                if contact_marked
                else "order.support_note.add"
            ),
            target_type="provider_order",
            target_id=order.order_no,
            after={
                "note_id": note.id,
                "content": note.content,
                "marks_customer_contact": contact_marked,
                "support_contacted_at": (
                    order.support_contacted_at.isoformat()
                    if order.support_contacted_at
                    else None
                ),
            },
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
            "refunded": summary_queryset.filter(
                status=ProviderOrderAfterSalesCase.Status.REFUNDED
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
        if serializer.validated_data["action"] == "retry_refund":
            from orders.models import ProviderOrderRefundOrder
            from orders.services import process_provider_order_refund

            refund = ProviderOrderRefundOrder.objects.filter(
                idempotency_key=f"provider-after-sales:{case.case_no}"
            ).first()
            if not refund:
                raise ValidationError("售后退款单不存在，请联系技术人员排查。")
            before = {
                "status": refund.status,
                "failure_reason": refund.failure_reason,
            }
            try:
                process_provider_order_refund(refund.refund_no)
            except Exception as exc:
                _provider_refund_retry_failed_response(
                    request=request,
                    access=access,
                    refund=refund,
                    action="order.after_sales.refund.retry.result",
                    before=before,
                    exception=exc,
                )
            refund.refresh_from_db()
            _record_refund_retry_audit(
                request=request,
                access=access,
                action="order.after_sales.refund.retry.result",
                target_type="provider_order_refund",
                target_id=refund.refund_no,
                before=before,
                after={
                    "status": refund.status,
                    "failure_reason": refund.failure_reason,
                },
            )
        case = get_object_or_404(provider_order_after_sales_queryset(access), pk=case.pk)
        return Response({"data": ProviderOrderAfterSalesCaseSerializer(case).data})


class OrganizationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        queryset = scoped_organizations(access).order_by("id")
        return Response({
            "data": {
                "items": AdminOrganizationSerializer(queryset, many=True).data,
            }
        })


class AdminPermissionCatalogView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        return Response({
            "data": {
                "groups": permission_catalog_data(),
                "data_scopes": [
                    {"value": value, "label": label}
                    for value, label in AdminRole.DataScope.choices
                ],
            }
        })


class AdminRoleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        queryset = scoped_admin_roles(access).annotate(
            member_count=Count("members")
        ).order_by("-is_system", "organization_id", "name")
        organization_id = request.query_params.get("organization", "").strip()
        if organization_id:
            queryset = queryset.filter(organization_id=organization_id)
        items = AdminRoleSerializer(queryset, many=True).data
        return Response({
            "data": {
                "items": items,
                "summary": {
                    "total": len(items),
                    "system": sum(1 for item in items if item["is_system"]),
                    "custom": sum(1 for item in items if not item["is_system"]),
                },
            }
        })

    @transaction.atomic
    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        serializer = AdminRoleMutationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = serializer.validated_data["organization"]
        if not scoped_organizations(access).filter(pk=organization.pk).exists():
            raise ValidationError("不能在当前数据范围外创建角色。")
        if (
            serializer.validated_data["data_scope"] == AdminRole.DataScope.ALL
            and not access.all_data
        ):
            raise ValidationError("当前账号不能授予全部数据范围。")
        role = serializer.save(is_system=False)
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=organization,
            action="system.role.create",
            target_type="admin_role",
            target_id=str(role.id),
            before={},
            after=role_audit_snapshot(role),
            ip_address=client_ip(request),
        )
        role.member_count = 0
        return Response(
            {"data": AdminRoleSerializer(role).data},
            status=201,
        )


class AdminRoleDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, access, role_id):
        return get_object_or_404(scoped_admin_roles(access), pk=role_id)

    @transaction.atomic
    def patch(self, request, role_id):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        role = self.get_object(access, role_id)
        if role.is_system:
            raise ValidationError("系统内置角色不可修改，可复制后创建自定义角色。")
        if access.member and access.member.role_id == role.id:
            raise ValidationError("不能修改当前账号正在使用的角色。")
        before = role_audit_snapshot(role)
        serializer = AdminRoleMutationSerializer(
            role,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        organization = serializer.validated_data.get("organization", role.organization)
        if organization.id != role.organization_id:
            raise ValidationError("角色创建后不能变更所属组织。")
        if (
            serializer.validated_data.get("data_scope", role.data_scope)
            == AdminRole.DataScope.ALL
            and not access.all_data
        ):
            raise ValidationError("当前账号不能授予全部数据范围。")
        next_permissions = serializer.validated_data.get("permissions", role.permissions)
        ensure_role_manager_remains(role, next_permissions)
        role = serializer.save()
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=role.organization,
            action="system.role.update",
            target_type="admin_role",
            target_id=str(role.id),
            before=before,
            after=role_audit_snapshot(role),
            ip_address=client_ip(request),
        )
        role.member_count = role.members.count()
        return Response({"data": AdminRoleSerializer(role).data})

    @transaction.atomic
    def delete(self, request, role_id):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        role = self.get_object(access, role_id)
        if role.is_system:
            raise ValidationError("系统内置角色不可删除。")
        if role.members.exists():
            raise ValidationError("该角色仍有后台账号使用，请先调整账号角色。")
        before = role_audit_snapshot(role)
        organization = role.organization
        target_id = str(role.id)
        role.delete()
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=organization,
            action="system.role.delete",
            target_type="admin_role",
            target_id=target_id,
            before=before,
            after={},
            ip_address=client_ip(request),
        )
        return Response(status=204)


class ProviderOrderingSettingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        if not access.all_data:
            raise PermissionDenied("只有平台管理员可以查看全局接单规则。")
        return Response({"data": ProviderOrderingSettingSerializer(ProviderOrderingSetting.current()).data})

    @transaction.atomic
    def patch(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        if not access.all_data:
            raise PermissionDenied("只有平台管理员可以修改全局接单规则。")
        setting = ProviderOrderingSetting.current()
        before = ProviderOrderingSettingSerializer(setting).data
        serializer = ProviderOrderingSettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=access.member.organization if access.member else None,
            action="operations.provider_ordering.update", target_type="operation_setting",
            target_id="provider-ordering", before=before, after=serializer.data,
            ip_address=client_ip(request),
        )
        return Response({"data": serializer.data})


class PlatformOperationSettingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        if not access.all_data:
            raise PermissionDenied("只有平台管理员可以查看全局运营参数。")
        setting = PlatformOperationSetting.current()
        return Response({"data": PlatformOperationSettingSerializer(setting).data})

    @transaction.atomic
    def patch(self, request):
        access = resolve_admin_access(request.user)
        access.require("operations.manage")
        if not access.all_data:
            raise PermissionDenied("只有平台管理员可以修改全局运营参数。")
        setting = PlatformOperationSetting.current()
        before = PlatformOperationSettingSerializer(setting).data
        serializer = PlatformOperationSettingSerializer(
            setting, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=access.member.organization if access.member else None,
            action="operations.platform.update",
            target_type="operation_setting",
            target_id="platform",
            before=before,
            after=serializer.data,
            ip_address=client_ip(request),
        )
        return Response({"data": serializer.data})


class OrganizationMemberListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        queryset = scoped_organization_members(access).order_by("-is_active", "id")
        keyword = request.query_params.get("search", "").strip()
        organization_id = request.query_params.get("organization", "").strip()
        role_id = request.query_params.get("role", "").strip()
        state = request.query_params.get("status", "").strip()
        if keyword:
            queryset = queryset.filter(
                Q(user__phone__icontains=keyword)
                | Q(user__nickname__icontains=keyword)
                | Q(role__name__icontains=keyword)
            )
        if organization_id:
            queryset = queryset.filter(organization_id=organization_id)
        if role_id:
            queryset = queryset.filter(role_id=role_id)
        if state == "active":
            queryset = queryset.filter(is_active=True)
        elif state == "inactive":
            queryset = queryset.filter(is_active=False)
        items = OrganizationMemberSerializer(
            queryset,
            many=True,
            context={"request": request},
        ).data
        return Response({
            "data": {
                "items": items,
                "summary": {
                    "total": len(items),
                    "active": sum(1 for item in items if item["is_active"]),
                    "inactive": sum(1 for item in items if not item["is_active"]),
                    "organizations": len({item["organization"] for item in items}),
                },
            }
        })

    @transaction.atomic
    def post(self, request):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        serializer = OrganizationMemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization = serializer.validated_data["organization"]
        role = serializer.validated_data["role"]
        if not scoped_organizations(access).filter(pk=organization.pk).exists():
            raise ValidationError("不能在当前数据范围外添加后台账号。")
        if not scoped_admin_roles(access).filter(pk=role.pk).exists():
            raise ValidationError("不能使用当前数据范围外的角色。")
        if role.data_scope == AdminRole.DataScope.ALL and not access.all_data:
            raise ValidationError("当前账号不能授予全部数据范围。")
        member = serializer.save()
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=member.organization,
            action="system.member.create",
            target_type="organization_member",
            target_id=str(member.id),
            before={},
            after=member_audit_snapshot(member),
            ip_address=client_ip(request),
        )
        return Response({
            "data": OrganizationMemberSerializer(
                member,
                context={"request": request},
            ).data,
        }, status=201)


class OrganizationMemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, access, member_id):
        return get_object_or_404(scoped_organization_members(access), pk=member_id)

    @transaction.atomic
    def patch(self, request, member_id):
        access = resolve_admin_access(request.user)
        access.require("organization.manage")
        member = self.get_object(access, member_id)
        if member.user_id == request.user.id:
            raise ValidationError("不能修改当前登录账号自身的角色、状态或数据范围。")
        before = member_audit_snapshot(member)
        serializer = OrganizationMemberUpdateSerializer(
            member,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        next_role = serializer.validated_data.get("role", member.role)
        next_active = serializer.validated_data.get("is_active", member.is_active)
        if not scoped_admin_roles(access).filter(pk=next_role.pk).exists():
            raise ValidationError("不能使用当前数据范围外的角色。")
        if next_role.data_scope == AdminRole.DataScope.ALL and not access.all_data:
            raise ValidationError("当前账号不能授予全部数据范围。")
        ensure_organization_manager_remains(member, next_role, next_active)
        member = serializer.save()
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=member.organization,
            action="system.member.update",
            target_type="organization_member",
            target_id=str(member.id),
            before=before,
            after=member_audit_snapshot(member),
            ip_address=client_ip(request),
        )
        return Response({
            "data": OrganizationMemberSerializer(
                member,
                context={"request": request},
            ).data,
        })


class AuditLogListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("audit.view")
        query = AuditLogQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        queryset = AdminAuditLog.objects.select_related("actor", "organization")
        if not access.all_data:
            queryset = queryset.filter(organization=access.member.organization)
        keyword = params.get("search", "").strip()
        action = params.get("action", "").strip()
        target_type = params.get("target_type", "").strip()
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
        page = params["page"]
        page_size = params["page_size"]
        start = (page - 1) * page_size
        items = queryset[start : start + page_size]
        return Response({"data": {"items": AuditLogSerializer(items, many=True).data, "pagination": {"page": page, "page_size": page_size, "total": total}}})


def scoped_scheduled_tasks(access):
    queryset = ScheduledTask.objects.all()
    if access.all_data:
        return queryset
    visible_order_nos = scoped_provider_orders(access).values("order_no")
    visible_provider_refund_nos = ProviderOrderRefundOrder.objects.filter(
        order__in=scoped_provider_orders(access)
    ).values("refund_no")
    visible_activities = scoped_activities(access)
    visible_activity_ids = visible_activities.annotate(
        task_business_key=Cast("id", output_field=CharField())
    ).values("task_business_key")
    visible_participation_order_nos = ActivityParticipationPaymentOrder.objects.filter(
        participation__activity__in=visible_activities,
    ).values("order_no")
    visible_publish_order_nos = ActivityPublishOrder.objects.filter(
        activity__in=visible_activities
    ).values("order_no")
    visible_participation_refund_nos = ActivityParticipationRefundOrder.objects.filter(
        activity__in=visible_activities
    ).values("refund_no")
    return queryset.filter(
        Q(
            business_type="provider_order",
            business_key__in=visible_order_nos,
        )
        | Q(
            business_type="provider_order_refund",
            business_key__in=visible_provider_refund_nos,
        )
        | Q(
            business_type="activity",
            business_key__in=visible_activity_ids,
        )
        | Q(
            business_type="activity_participation",
            business_key__in=visible_participation_order_nos,
        )
        | Q(
            business_type="activity_publish_payment",
            business_key__in=visible_publish_order_nos,
        )
        | Q(
            business_type="activity_participation_refund",
            business_key__in=visible_participation_refund_nos,
        )
    )


class ScheduledTaskListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        access = resolve_admin_access(request.user)
        access.require("system.task.view")
        query = ScheduledTaskQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        scoped = scoped_scheduled_tasks(access)
        now = timezone.now()
        today = timezone.localdate()
        summary = {
            "total": scoped.count(),
            "pending": scoped.filter(status=ScheduledTask.Status.PENDING).count(),
            "running": scoped.filter(status=ScheduledTask.Status.RUNNING).count(),
            "succeeded_today": scoped.filter(
                status=ScheduledTask.Status.SUCCEEDED,
                finished_at__date=today,
            ).count(),
            "failed": scoped.filter(status=ScheduledTask.Status.FAILED).count(),
            "overdue": scoped.filter(
                Q(
                    status=ScheduledTask.Status.PENDING,
                    available_at__lte=now - timedelta(minutes=1),
                )
                | Q(
                    status=ScheduledTask.Status.RUNNING,
                    started_at__lte=now - TASK_LEASE_TIMEOUT,
                )
            ).count(),
        }
        queryset = scoped
        if task_type := data.get("task_type"):
            queryset = queryset.filter(task_type=task_type)
        if task_status := data.get("status"):
            queryset = queryset.filter(status=task_status)
        if data.get("overdue"):
            queryset = queryset.filter(
                Q(
                    status=ScheduledTask.Status.PENDING,
                    available_at__lte=now - timedelta(minutes=1),
                )
                | Q(
                    status=ScheduledTask.Status.RUNNING,
                    started_at__lte=now - TASK_LEASE_TIMEOUT,
                )
            )
        if keyword := data.get("search", "").strip():
            queryset = queryset.filter(
                Q(business_key__icontains=keyword)
                | Q(dedupe_key__icontains=keyword)
            )
        page = data["page"]
        page_size = data["page_size"]
        total = queryset.count()
        start = (page - 1) * page_size
        items = queryset[start : start + page_size]
        return Response({
            "data": {
                "items": ScheduledTaskSerializer(items, many=True).data,
                "pagination": {"page": page, "page_size": page_size, "total": total},
                "summary": summary,
                "task_types": [
                    {"value": value, "label": label}
                    for value, label in ScheduledTask.Type.choices
                ],
                "statuses": [
                    {"value": value, "label": label}
                    for value, label in ScheduledTask.Status.choices
                ],
            }
        })


class ScheduledTaskDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, public_id):
        access = resolve_admin_access(request.user)
        access.require("system.task.view")
        task = get_object_or_404(scoped_scheduled_tasks(access), public_id=public_id)
        return Response({"data": ScheduledTaskSerializer(task).data})


class ScheduledTaskRetryView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, public_id):
        access = resolve_admin_access(request.user)
        access.require("system.task.retry")
        task = get_object_or_404(
            scoped_scheduled_tasks(access).select_for_update(),
            public_id=public_id,
        )
        before = ScheduledTaskSerializer(task).data
        try:
            retry_failed_task(task)
        except ValueError as exc:
            raise ValidationError({"status": str(exc)}) from exc
        AdminAuditLog.objects.create(
            actor=request.user,
            organization=access.member.organization if access.member else None,
            action="system.task.retry",
            target_type="scheduled_task",
            target_id=str(task.public_id),
            before={
                "status": before["status"],
                "attempt_count": before["attempt_count"],
                "last_error": before["last_error"],
            },
            after={
                "status": task.status,
                "attempt_count": task.attempt_count,
                "available_at": task.available_at.isoformat(),
            },
            ip_address=client_ip(request),
        )
        return Response({"data": ScheduledTaskSerializer(task).data})
