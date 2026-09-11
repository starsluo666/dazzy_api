from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from activities.models import (
    Activity,
    ActivityAfterSalesCase,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivityRefundRecord,
    ActivityReport,
    ActivitySettlement,
)
from activities.services import (
    advance_activity_settlement,
    create_activity_participation_refund,
    release_activity_settlement_after_sales,
    refund_all_activity_participations,
    refund_publish_order,
    sync_activity_formation_status,
)
from notifications.models import UserNotification
from notifications.services import (
    create_activity_notification,
    create_activity_notifications,
    create_order_notification,
    create_system_notification,
)
from orders.models import (
    ProviderOrder,
    ProviderOrderRefundOrder,
    ProviderOrderReview,
    ProviderOrderSettlement,
)
from orders.services import create_provider_order_refund, refresh_provider_review_metrics
from providers.models import ProviderLiveLocation, ProviderProfile
from taskcenter.services import (
    cancel_provider_order_settlement,
    cancel_provider_order_confirmation_timeout,
    reopen_provider_order_settlement,
    reopen_provider_order_confirmation_timeout,
)

from .access import client_ip
from .models import (
    AdminAuditLog,
    ProviderCreditAdjustment,
    ProviderOrderAfterSalesCase,
    UserRiskFlag,
)


def _organization(access):
    return access.member.organization if access.member else None


def _scoped_users(access):
    queryset = User.objects.filter(is_superuser=False, backoffice_memberships__isnull=True)
    if not access.all_data:
        queryset = queryset.filter(
            Q(provider_orders__provider__service_city_code__in=access.city_codes)
            | Q(provider_profile__service_city_code__in=access.city_codes)
        )
    return queryset.distinct()


@transaction.atomic
def review_activity(*, activity_id, decision, reason, actor, access, request):
    queryset = Activity.objects.select_for_update().select_related(
        "category", "organizer"
    )
    if not access.all_data:
        queryset = queryset.filter(city_code__in=access.city_codes)
    activity = get_object_or_404(queryset, id=activity_id)
    if activity.status != Activity.Status.PENDING_REVIEW:
        raise ValidationError("仅待审核活动可以执行审核。")

    publish_order = (
        ActivityPublishOrder.objects.select_for_update()
        .filter(activity=activity)
        .order_by("-created_at")
        .first()
    )
    if not publish_order or publish_order.status != ActivityPublishOrder.Status.PAID:
        raise ValidationError("活动发布支付单未支付，暂不能审核。")

    now = timezone.now()
    if decision == "approve":
        if not activity.category.is_active:
            raise ValidationError({"decision": "活动分类已停用，不能通过审核。"})
        if activity.organizer.account_status != User.AccountStatus.ACTIVE:
            raise ValidationError({"decision": "发起人账号当前不可用。"})
        if activity.formation_deadline <= now or activity.starts_at <= now:
            raise ValidationError({"decision": "活动报名或开始时间已经过期。"})

    before = {
        "status": activity.status,
        "rejection_reason": activity.rejection_reason,
        "publish_order_status": publish_order.status,
    }
    activity.status = (
        Activity.Status.RECRUITING
        if decision == "approve"
        else Activity.Status.REJECTED
    )
    activity.reviewed_by = actor
    activity.reviewed_at = now
    activity.rejection_reason = reason.strip() if decision == "reject" else ""
    activity.published_at = now if decision == "approve" else None
    activity.save(
        update_fields=(
            "status", "reviewed_by", "reviewed_at", "rejection_reason",
            "published_at", "updated_at",
        )
    )
    if decision == "approve":
        from taskcenter.services import register_activity_lifecycle_tasks

        register_activity_lifecycle_tasks(activity)
    if decision == "reject":
        refund_publish_order(
            activity=activity,
            publish_order=publish_order,
            refund_type=ActivityRefundRecord.RefundType.REVIEW_REJECTION,
            reason=activity.rejection_reason,
            operator=actor,
        )

    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"activity.review.{decision}",
        target_type="activity",
        target_id=str(activity.id),
        before=before,
        after={
            "status": activity.status,
            "rejection_reason": activity.rejection_reason,
            "publish_order_status": publish_order.status,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    create_activity_notification(
        activity=activity,
        recipient=activity.organizer,
        event_type=UserNotification.EventType.ACTIVITY_REVIEW_RESULT,
        title="活动审核通过" if decision == "approve" else "活动审核未通过",
        content=(
            "活动已经发布并开始接受报名。"
            if decision == "approve"
            else f"活动未通过审核：{activity.rejection_reason}。发布支付已按规则退款。"
        ),
        dedupe_suffix=decision,
    )
    return activity


@transaction.atomic
def cancel_activity_by_admin(*, activity_id, reason, actor, access, request):
    queryset = Activity.objects.select_for_update().select_related("organizer")
    if not access.all_data:
        queryset = queryset.filter(city_code__in=access.city_codes)
    activity = get_object_or_404(queryset, id=activity_id)
    if activity.status not in (Activity.Status.RECRUITING, Activity.Status.FORMED):
        raise ValidationError("仅报名中或已成局活动可以由后台取消。")
    publish_order = (
        ActivityPublishOrder.objects.select_for_update()
        .filter(activity=activity)
        .order_by("-created_at")
        .first()
    )
    if not publish_order or publish_order.status != ActivityPublishOrder.Status.PAID:
        raise ValidationError("活动发布支付单不在可退款状态。")

    before = {"status": activity.status, "cancellation_reason": activity.cancellation_reason}
    now = timezone.now()
    participant_users = [
        item.user
        for item in ActivityParticipation.objects.filter(
            activity=activity,
            status=ActivityParticipation.Status.ACTIVE,
        ).select_related("user")
    ]
    activity.status = Activity.Status.CANCELLED
    activity.cancellation_reason = reason
    activity.cancelled_by = actor
    activity.cancelled_at = now
    activity.save(
        update_fields=(
            "status", "cancellation_reason", "cancelled_by", "cancelled_at", "updated_at",
        )
    )
    participation_refunds = refund_all_activity_participations(
        activity=activity,
        refund_type=ActivityParticipationRefundOrder.RefundType.ADMIN_CANCELLATION,
        reason=reason,
        cancelled_by_role=ActivityParticipation.CancelledByRole.PLATFORM,
        operator=actor,
    )
    refund = refund_publish_order(
        activity=activity,
        publish_order=publish_order,
        refund_type=ActivityRefundRecord.RefundType.ADMIN_CANCELLATION,
        reason=reason,
        operator=actor,
    )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action="activity.management.cancel",
        target_type="activity",
        target_id=str(activity.id),
        before=before,
        after={
            "status": activity.status,
            "cancellation_reason": activity.cancellation_reason,
            "refund_no": refund.refund_no,
            "refund_amount": refund.refund_amount,
            "participation_refund_nos": [item.refund_no for item in participation_refunds],
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    create_activity_notifications(
        activity=activity,
        recipients=[activity.organizer, *participant_users],
        event_type=UserNotification.EventType.ACTIVITY_CANCELLED,
        title="活动已被平台取消",
        content=f"活动已取消：{reason}。相关退款记录可在活动详情查看。",
        dedupe_suffix="admin",
    )
    return activity


@transaction.atomic
def review_activity_after_sales_case(
    *,
    case_no,
    action,
    result_note,
    approved_principal_amount,
    approved_service_fee_amount,
    actor,
    access,
    request,
):
    queryset = ActivityAfterSalesCase.objects.select_for_update().select_related(
        "participation__activity", "participation__user"
    )
    if not access.all_data:
        queryset = queryset.filter(
            participation__activity__city_code__in=access.city_codes
        )
    case = get_object_or_404(queryset, case_no=case_no)
    transitions = {
        "start_review": (
            (ActivityAfterSalesCase.Status.PENDING,),
            ActivityAfterSalesCase.Status.PROCESSING,
        ),
        "approve": (
            (
                ActivityAfterSalesCase.Status.PENDING,
                ActivityAfterSalesCase.Status.PROCESSING,
            ),
            ActivityAfterSalesCase.Status.APPROVED,
        ),
        "reject": (
            (
                ActivityAfterSalesCase.Status.PENDING,
                ActivityAfterSalesCase.Status.PROCESSING,
            ),
            ActivityAfterSalesCase.Status.REJECTED,
        ),
    }
    allowed, target = transitions[action]
    if case.status not in allowed:
        raise ValidationError("当前售后状态不能执行该操作。")
    before = {"status": case.status, "result_note": case.result_note}
    refund = None
    if action == "approve":
        settlement = ActivitySettlement.objects.select_for_update().filter(
            activity=case.participation.activity
        ).first()
        if settlement and settlement.status == ActivitySettlement.Status.SETTLED:
            raise ValidationError("活动资金已经结算，不能再批准退款。")
        principal_amount = (
            case.requested_principal_amount
            if approved_principal_amount is None
            else approved_principal_amount
        )
        service_fee_amount = (
            case.requested_service_fee_amount
            if approved_service_fee_amount is None
            else approved_service_fee_amount
        )
        if principal_amount > case.requested_principal_amount:
            raise ValidationError("核准AA本金退款不能超过申请金额。")
        if service_fee_amount > case.requested_service_fee_amount:
            raise ValidationError("核准平台服务费退款不能超过申请金额。")
        payment_order = case.participation.payment_orders.select_for_update().filter(
            status__in=(
                ActivityParticipationPaymentOrder.Status.PAID,
                ActivityParticipationPaymentOrder.Status.PARTIALLY_REFUNDED,
            )
        ).first()
        if not payment_order:
            raise ValidationError("售后单缺少可退款支付单。")
        refund, _ = create_activity_participation_refund(
            participation=case.participation,
            payment_order=payment_order,
            refund_type=ActivityParticipationRefundOrder.RefundType.AFTER_SALES,
            idempotency_key=f"after-sales:{case.case_no}",
            principal_refund_amount=principal_amount,
            service_fee_refund_amount=service_fee_amount,
            reason=result_note,
            operator=actor,
        )
        case.approved_principal_amount = principal_amount
        case.approved_service_fee_amount = service_fee_amount
        case.approved_amount = principal_amount + service_fee_amount
        case.refund_order = refund
        participation = case.participation
        if participation.status == ActivityParticipation.Status.ACTIVE:
            participation.status = ActivityParticipation.Status.CANCELLED
            participation.cancelled_at = timezone.now()
            participation.cancellation_reason = result_note
            participation.cancelled_by_role = ActivityParticipation.CancelledByRole.PLATFORM
            participation.save(update_fields=(
                "status", "cancelled_at", "cancellation_reason",
                "cancelled_by_role", "updated_at",
            ))
            participant_count = ActivityParticipation.objects.filter(
                activity=participation.activity,
                status=ActivityParticipation.Status.ACTIVE,
            ).count()
            sync_activity_formation_status(participation.activity, participant_count)
    case.status = target
    case.result_note = result_note.strip() if action != "start_review" else ""
    case.reviewed_by = actor
    case.reviewed_at = timezone.now()
    case.save(update_fields=(
        "status", "result_note", "reviewed_by", "reviewed_at",
        "approved_principal_amount", "approved_service_fee_amount", "approved_amount",
        "refund_order", "updated_at",
    ))
    if action in ("approve", "reject"):
        release_activity_settlement_after_sales(
            activity=case.participation.activity,
            now=case.reviewed_at,
        )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"activity.after_sales.{action}",
        target_type="activity_after_sales_case",
        target_id=case.case_no,
        before=before,
        after={
            "status": case.status,
            "approved_amount": case.approved_amount,
            "refund_no": refund.refund_no if refund else None,
            "result_note": case.result_note,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    if action in ("approve", "reject"):
        create_activity_notification(
            activity=case.participation.activity,
            recipient=case.applicant,
            event_type=UserNotification.EventType.ACTIVITY_AFTER_SALES_RESULT,
            title="活动售后已同意" if action == "approve" else "活动售后已驳回",
            content=case.result_note or "活动售后已处理，请查看详情。",
            dedupe_suffix=f"{case.case_no}:{action}",
        )
    return case


@transaction.atomic
def review_activity_settlement(
    *, settlement_no, action, reason, actor, access, request
):
    queryset = ActivitySettlement.objects.select_for_update().select_related(
        "activity", "beneficiary"
    )
    if not access.all_data:
        queryset = queryset.filter(activity__city_code__in=access.city_codes)
    settlement = get_object_or_404(queryset, settlement_no=settlement_no)
    before = {
        "status": settlement.status,
        "dispute_source": settlement.dispute_source,
        "dispute_reason": settlement.dispute_reason,
    }
    now = timezone.now()
    if action == "freeze_dispute":
        if settlement.status == ActivitySettlement.Status.SETTLED:
            raise ValidationError("已结算资金不能再冻结。")
        settlement.status = ActivitySettlement.Status.DISPUTE_FROZEN
        settlement.dispute_source = ActivitySettlement.DisputeSource.ADMIN
        settlement.dispute_reason = reason.strip()
        settlement.save(update_fields=(
            "status", "dispute_source", "dispute_reason", "updated_at",
        ))
    elif action == "release_dispute":
        if settlement.dispute_source != ActivitySettlement.DisputeSource.ADMIN:
            raise ValidationError("当前结算单不是后台风控冻结状态。")
        if ActivityAfterSalesCase.objects.filter(
            participation__activity=settlement.activity,
            status__in=(
                ActivityAfterSalesCase.Status.PENDING,
                ActivityAfterSalesCase.Status.PROCESSING,
            ),
        ).exists():
            raise ValidationError("仍有未处理售后，暂不能解除冻结。")
        settlement.dispute_source = ""
        settlement.dispute_reason = ""
        if now < settlement.confirmation_deadline:
            settlement.status = ActivitySettlement.Status.CONFIRMING
            settlement.risk_frozen_at = None
        else:
            settlement.status = ActivitySettlement.Status.RISK_FROZEN
            settlement.risk_frozen_at = settlement.confirmation_deadline
        settlement.save(update_fields=(
            "status", "risk_frozen_at", "dispute_source", "dispute_reason",
            "updated_at",
        ))
        settlement, _ = advance_activity_settlement(
            settlement_id=settlement.pk, now=now
        )
    else:
        if settlement.dispute_source == ActivitySettlement.DisputeSource.ADMIN:
            raise ValidationError("请先解除后台风控冻结。")
        settlement, _ = advance_activity_settlement(
            settlement_id=settlement.pk, now=now
        )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"activity.settlement.{action}",
        target_type="activity_settlement",
        target_id=settlement.settlement_no,
        before=before,
        after={
            "status": settlement.status,
            "dispute_source": settlement.dispute_source,
            "dispute_reason": settlement.dispute_reason,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    return settlement


@transaction.atomic
def review_activity_report(*, case_no, action, result_note, actor, access, request):
    queryset = ActivityReport.objects.select_for_update().select_related("activity")
    if not access.all_data:
        queryset = queryset.filter(activity__city_code__in=access.city_codes)
    report = get_object_or_404(queryset, case_no=case_no)
    transitions = {
        "start_review": ((ActivityReport.Status.PENDING,), ActivityReport.Status.PROCESSING),
        "resolve": ((ActivityReport.Status.PENDING, ActivityReport.Status.PROCESSING), ActivityReport.Status.RESOLVED),
        "reject": ((ActivityReport.Status.PENDING, ActivityReport.Status.PROCESSING), ActivityReport.Status.REJECTED),
    }
    allowed, target = transitions[action]
    if report.status not in allowed:
        raise ValidationError("当前举报状态不能执行该操作。")
    before = {"status": report.status, "result_note": report.result_note}
    report.status = target
    report.reviewed_by = actor
    report.reviewed_at = timezone.now()
    report.result_note = result_note.strip() if action != "start_review" else ""
    report.save(
        update_fields=("status", "reviewed_by", "reviewed_at", "result_note", "updated_at")
    )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"activity.report.{action}",
        target_type="activity_report",
        target_id=report.case_no,
        before=before,
        after={"status": report.status, "result_note": report.result_note},
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    return report


@transaction.atomic
def review_provider_application(*, profile_id, decision, reason, actor, access, request):
    queryset = ProviderProfile.objects.select_for_update().select_related("user")
    if not access.all_data:
        queryset = queryset.filter(service_city_code__in=access.city_codes)
    profile = get_object_or_404(queryset, id=profile_id)
    if profile.status != ProviderProfile.Status.PENDING:
        raise ValidationError("仅待审核申请可以执行审核。")
    before = {"status": profile.status, "rejection_reason": profile.rejection_reason}
    profile.status = (
        ProviderProfile.Status.APPROVED
        if decision == "approve"
        else ProviderProfile.Status.REJECTED
    )
    profile.reviewed_at = timezone.now()
    profile.rejection_reason = reason.strip() if decision == "reject" else ""
    if decision == "approve":
        profile.is_accepting_orders = False
    profile.save(
        update_fields=(
            "status",
            "reviewed_at",
            "rejection_reason",
            "is_accepting_orders",
            "updated_at",
        )
    )
    organization = access.member.organization if access.member else None
    AdminAuditLog.objects.create(
        actor=actor,
        organization=organization,
        action=f"provider.application.{decision}",
        target_type="provider_profile",
        target_id=str(profile.id),
        before=before,
        after={"status": profile.status, "rejection_reason": profile.rejection_reason},
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    create_system_notification(
        recipient=profile.user,
        event_type=UserNotification.EventType.PROVIDER_APPLICATION_RESULT,
        title="达人申请审核通过" if decision == "approve" else "达人申请审核未通过",
        content=(
            "你的达人申请已通过，可以进入达人端完成实名认证并完善资料。"
            if decision == "approve"
            else f"你的达人申请未通过：{profile.rejection_reason}。修改资料后可重新提交。"
        ),
        action_text="查看申请",
        action_url="/pages/providers/apply",
        dedupe_key=f"provider-application:{profile.pk}:{decision}:{profile.reviewed_at.isoformat()}",
    )
    return profile


@transaction.atomic
def review_provider_identity(*, profile_id, decision, reason, actor, access, request):
    queryset = ProviderProfile.objects.select_for_update().select_related("user")
    if not access.all_data:
        queryset = queryset.filter(service_city_code__in=access.city_codes)
    profile = get_object_or_404(queryset, id=profile_id)
    if profile.status != ProviderProfile.Status.APPROVED:
        raise ValidationError("仅入驻申请已通过的达人可以审核实名认证。")
    if profile.identity_status != ProviderProfile.IdentityStatus.PENDING:
        raise ValidationError("仅认证中的实名认证可以执行审核。")
    before = {
        "identity_status": profile.identity_status,
        "identity_rejection_reason": profile.identity_rejection_reason,
    }
    profile.identity_status = (
        ProviderProfile.IdentityStatus.VERIFIED
        if decision == "approve"
        else ProviderProfile.IdentityStatus.REJECTED
    )
    profile.identity_reviewed_at = timezone.now()
    profile.identity_rejection_reason = reason.strip() if decision == "reject" else ""
    if decision == "reject":
        profile.is_accepting_orders = False
    profile.save(
        update_fields=(
            "identity_status",
            "identity_reviewed_at",
            "identity_rejection_reason",
            "is_accepting_orders",
            "updated_at",
        )
    )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"provider.identity.{decision}",
        target_type="provider_profile",
        target_id=str(profile.id),
        before=before,
        after={
            "identity_status": profile.identity_status,
            "identity_rejection_reason": profile.identity_rejection_reason,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    create_system_notification(
        recipient=profile.user,
        event_type=UserNotification.EventType.PROVIDER_STATUS_CHANGED,
        title="达人实名认证已通过" if decision == "approve" else "达人实名认证未通过",
        content=(
            "实名认证已通过，完善达人资料和服务后即可开启接单。"
            if decision == "approve"
            else f"实名认证未通过：{profile.identity_rejection_reason}。请修改后重新提交。"
        ),
        dedupe_key=f"provider-identity:{profile.pk}:{decision}:{profile.identity_reviewed_at.isoformat()}",
    )
    return profile


@transaction.atomic
def change_user_account_status(*, public_id, action, reason, actor, access, request):
    scoped_ids = _scoped_users(access).filter(public_id=public_id).values("pk")
    user = get_object_or_404(
        User.objects.select_for_update().filter(pk__in=scoped_ids),
        public_id=public_id,
    )
    if user.account_status == User.AccountStatus.CLOSED:
        raise ValidationError("已注销账号不能由运营后台恢复或变更。")
    transitions = {
        "restrict": (User.AccountStatus.ACTIVE, User.AccountStatus.RESTRICTED),
        "suspend": (
            (User.AccountStatus.ACTIVE, User.AccountStatus.RESTRICTED),
            User.AccountStatus.SUSPENDED,
        ),
        "restore": (
            (User.AccountStatus.RESTRICTED, User.AccountStatus.SUSPENDED),
            User.AccountStatus.ACTIVE,
        ),
    }
    expected, target = transitions[action]
    allowed = expected if isinstance(expected, tuple) else (expected,)
    if user.account_status not in allowed:
        raise ValidationError("当前账号状态不能执行该操作。")
    before = {"account_status": user.account_status, "auth_version": user.auth_version}
    user.account_status = target
    user.auth_version += 1
    user.save(update_fields=("account_status", "auth_version"))
    if target != User.AccountStatus.ACTIVE:
        ProviderProfile.objects.filter(user=user).update(is_accepting_orders=False)
        ProviderLiveLocation.objects.filter(provider__user=user).update(session_id=None)
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"user.account.{action}",
        target_type="user",
        target_id=str(user.public_id),
        before=before,
        after={
            "account_status": user.account_status,
            "auth_version": user.auth_version,
            "reason": reason,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    return user


@transaction.atomic
def change_user_risk_flag(*, public_id, action, level, reason, actor, access, request):
    scoped_ids = _scoped_users(access).filter(public_id=public_id).values("pk")
    user = get_object_or_404(
        User.objects.select_for_update().filter(pk__in=scoped_ids),
        public_id=public_id,
    )
    flag = UserRiskFlag.objects.select_for_update().filter(user=user).first()
    before = (
        {"is_active": flag.is_active, "level": flag.level, "reason": flag.reason}
        if flag
        else {}
    )
    now = timezone.now()
    if action == "mark":
        if flag is None:
            flag = UserRiskFlag.objects.create(
                user=user,
                level=level,
                reason=reason,
                marked_by=actor,
                organization=_organization(access),
            )
        else:
            flag.level = level
            flag.reason = reason
            flag.is_active = True
            flag.marked_by = actor
            flag.organization = _organization(access)
            flag.marked_at = now
            flag.cleared_by = None
            flag.cleared_at = None
            flag.save()
    else:
        if flag is None or not flag.is_active:
            raise ValidationError("该用户当前没有生效中的风险标记。")
        flag.is_active = False
        flag.cleared_by = actor
        flag.cleared_at = now
        flag.save(update_fields=("is_active", "cleared_by", "cleared_at", "updated_at"))
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"user.risk.{action}",
        target_type="user",
        target_id=str(user.public_id),
        before=before,
        after={
            "is_active": flag.is_active,
            "level": flag.level,
            "reason": flag.reason,
            "operation_reason": reason,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    return user


@transaction.atomic
def change_provider_operational_status(*, profile_id, action, reason, actor, access, request):
    queryset = ProviderProfile.objects.select_for_update().select_related("user")
    if not access.all_data:
        queryset = queryset.filter(service_city_code__in=access.city_codes)
    profile = get_object_or_404(queryset, id=profile_id)
    before = {
        "status": profile.status,
        "is_accepting_orders": profile.is_accepting_orders,
        "admin_order_restricted": profile.admin_order_restricted,
        "admin_restriction_reason": profile.admin_restriction_reason,
    }
    if action == "restrict_orders":
        if profile.status != ProviderProfile.Status.APPROVED or profile.admin_order_restricted:
            raise ValidationError("仅正常且未受限的达人可以限制接单。")
        profile.is_accepting_orders = False
        profile.admin_order_restricted = True
        profile.admin_restriction_reason = reason
    elif action == "resume_orders":
        if profile.status != ProviderProfile.Status.APPROVED or not profile.admin_order_restricted:
            raise ValidationError("仅接单受限的正常达人可以恢复资格。")
        profile.admin_order_restricted = False
        profile.admin_restriction_reason = ""
        profile.is_accepting_orders = False
    elif action == "suspend_qualification":
        if profile.status != ProviderProfile.Status.APPROVED:
            raise ValidationError("仅审核通过的达人可以暂停资格。")
        profile.status = ProviderProfile.Status.SUSPENDED
        profile.is_accepting_orders = False
        profile.admin_order_restricted = True
        profile.admin_restriction_reason = reason
    else:
        if profile.status != ProviderProfile.Status.SUSPENDED:
            raise ValidationError("仅已暂停的达人可以恢复资格。")
        profile.status = ProviderProfile.Status.APPROVED
        profile.is_accepting_orders = False
        profile.admin_order_restricted = False
        profile.admin_restriction_reason = ""
    profile.save(
        update_fields=(
            "status", "is_accepting_orders", "admin_order_restricted",
            "admin_restriction_reason", "updated_at",
        )
    )
    if not profile.is_accepting_orders:
        ProviderLiveLocation.objects.filter(provider=profile).update(session_id=None)
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"provider.management.{action}",
        target_type="provider_profile",
        target_id=str(profile.id),
        before=before,
        after={
            "status": profile.status,
            "is_accepting_orders": profile.is_accepting_orders,
            "admin_order_restricted": profile.admin_order_restricted,
            "admin_restriction_reason": profile.admin_restriction_reason,
            "operation_reason": reason,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    status_messages = {
        "restrict_orders": ("接单资格已受限", f"平台已限制你的接单资格：{reason}"),
        "resume_orders": ("接单资格已恢复", "平台已恢复你的接单资格，可在达人端重新开启接单。"),
        "suspend_qualification": ("达人资格已暂停", f"平台已暂停你的达人资格：{reason}"),
        "restore_qualification": ("达人资格已恢复", "平台已恢复你的达人资格，可在达人端重新开启接单。"),
    }
    title, content = status_messages[action]
    create_system_notification(
        recipient=profile.user,
        event_type=UserNotification.EventType.PROVIDER_STATUS_CHANGED,
        title=title,
        content=content,
        dedupe_key=f"provider-status:{profile.pk}:{action}:{profile.updated_at.isoformat()}",
    )
    return profile


@transaction.atomic
def adjust_provider_credit(*, profile_id, delta, reason, actor, access, request):
    queryset = ProviderProfile.objects.select_for_update().select_related("user")
    if not access.all_data:
        queryset = queryset.filter(service_city_code__in=access.city_codes)
    profile = get_object_or_404(queryset, id=profile_id)
    if profile.status not in (ProviderProfile.Status.APPROVED, ProviderProfile.Status.SUSPENDED):
        raise ValidationError("仅已通过或已暂停的达人可以调整信用分。")
    before_score = profile.credit_score
    after_score = before_score + delta
    if not 0 <= after_score <= 100:
        raise ValidationError({"delta": "调整后信用分必须在 0–100 分之间。"})
    profile.credit_score = after_score
    profile.save(update_fields=("credit_score", "updated_at"))
    adjustment = ProviderCreditAdjustment.objects.create(
        provider=profile,
        operator=actor,
        organization=_organization(access),
        delta=delta,
        before_score=before_score,
        after_score=after_score,
        reason=reason,
    )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action="provider.credit.adjust",
        target_type="provider_profile",
        target_id=str(profile.id),
        before={"credit_score": before_score},
        after={
            "credit_score": after_score,
            "delta": delta,
            "reason": reason,
            "adjustment_id": adjustment.id,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    direction = "增加" if delta > 0 else "扣减"
    create_system_notification(
        recipient=profile.user,
        event_type=UserNotification.EventType.PROVIDER_CREDIT_CHANGED,
        title="达人信用分已变更",
        content=(
            f"信用分{direction} {abs(delta)} 分，当前为 {after_score} 分。原因：{reason}"
        ),
        dedupe_key=f"provider-credit:{adjustment.pk}",
    )
    return profile


def _scoped_provider_orders(access):
    queryset = ProviderOrder.objects.all()
    if not access.all_data:
        queryset = queryset.filter(provider__service_city_code__in=access.city_codes)
    return queryset


@transaction.atomic
def moderate_provider_order_review(
    *, order_no, action, reason, actor, access, request
):
    order = get_object_or_404(
        _scoped_provider_orders(access).select_for_update(), order_no=order_no
    )
    review = get_object_or_404(
        ProviderOrderReview.objects.select_for_update().select_related("provider"),
        order=order,
    )
    before_visible = review.is_visible
    review.is_visible = action == "restore"
    if review.is_visible != before_visible:
        review.save(update_fields=("is_visible", "updated_at"))
        refresh_provider_review_metrics(review.provider)
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=f"order.review.{action}",
        target_type="provider_order_review",
        target_id=str(review.id),
        before={"order_no": order.order_no, "is_visible": before_visible},
        after={
            "order_no": order.order_no,
            "is_visible": review.is_visible,
            "reason": reason.strip(),
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    return order


@transaction.atomic
def create_provider_order_after_sales_case(
    *, order_no, case_type, requested_amount, reason, actor, access, request
):
    order = get_object_or_404(
        _scoped_provider_orders(access).select_for_update(), order_no=order_no
    )
    if not order.paid_at:
        raise ValidationError("未支付订单不能登记退款或售后。")
    if order.status == ProviderOrder.Status.REFUNDED:
        raise ValidationError("该订单已经退款，不能重复登记售后。")
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if settlement and settlement.status == ProviderOrderSettlement.Status.SETTLED:
        raise ValidationError("该订单资金已经结算，请转异常交易流程处理。")
    if case_type == ProviderOrderAfterSalesCase.CaseType.REFUND and requested_amount <= 0:
        raise ValidationError({"requested_amount": "退款申请金额必须大于 0。"})
    if requested_amount > order.payable_amount:
        raise ValidationError({"requested_amount": "申请金额不能超过订单实付金额。"})
    if order.after_sales_cases.filter(
        status__in=(
            ProviderOrderAfterSalesCase.Status.PENDING,
            ProviderOrderAfterSalesCase.Status.PROCESSING,
            ProviderOrderAfterSalesCase.Status.APPROVED,
        )
    ).exists():
        raise ValidationError("该订单已有未结束的退款或售后单。")
    original_status = order.status
    try:
        with transaction.atomic():
            case = ProviderOrderAfterSalesCase.objects.create(
                order=order,
                creator=actor,
                organization=_organization(access),
                case_type=case_type,
                original_order_status=original_status,
                requested_amount=requested_amount,
                reason=reason,
            )
    except IntegrityError as error:
        raise ValidationError("该订单已有未结束的退款或售后单。") from error
    order.status = ProviderOrder.Status.AFTER_SALES
    order.save(update_fields=("status", "updated_at"))
    if settlement and settlement.status == ProviderOrderSettlement.Status.RISK_FROZEN:
        settlement.status = ProviderOrderSettlement.Status.DISPUTE_FROZEN
        settlement.dispute_reason = f"存在待处理退款售后：{case.case_no}"
        settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
        cancel_provider_order_settlement(order.order_no, "after_sales_processing")
    if original_status == ProviderOrder.Status.PENDING_CONFIRMATION:
        cancel_provider_order_confirmation_timeout(
            order.order_no, "after_sales_processing"
        )
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action="order.after_sales.create",
        target_type="provider_order_after_sales_case",
        target_id=case.case_no,
        before={"order_status": original_status},
        after={
            "order_no": order.order_no,
            "order_status": order.status,
            "case_type": case.case_type,
            "requested_amount": case.requested_amount,
            "reason": case.reason,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    create_order_notification(
        order=order,
        event_type=UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
        title="订单已进入售后处理",
        content="平台已登记退款/售后申请，处理结果会通过通知中心告知你。",
        dedupe_suffix=case.case_no,
    )
    return case


@transaction.atomic
def review_provider_order_after_sales_case(
    *, case_no, action, approved_amount, result_note, actor, access, request
):
    queryset = ProviderOrderAfterSalesCase.objects.select_for_update().select_related("order")
    if not access.all_data:
        queryset = queryset.filter(order__provider__service_city_code__in=access.city_codes)
    case = get_object_or_404(queryset, case_no=case_no)
    order = ProviderOrder.objects.select_for_update().get(pk=case.order_id)
    before = {
        "case_status": case.status,
        "order_status": order.status,
        "approved_amount": case.approved_amount,
    }
    if action == "start_review":
        if case.status != ProviderOrderAfterSalesCase.Status.PENDING:
            raise ValidationError("仅待处理售后单可以开始处理。")
        case.status = ProviderOrderAfterSalesCase.Status.PROCESSING
        audit_action = "order.after_sales.start_review"
    elif action == "retry_refund":
        if case.status != ProviderOrderAfterSalesCase.Status.APPROVED:
            raise ValidationError("仅退款失败或待退款的售后单可以重试。")
        refund = ProviderOrderRefundOrder.objects.filter(
            idempotency_key=f"provider-after-sales:{case.case_no}"
        ).first()
        from orders.services import provider_order_refund_can_retry

        if not refund or not provider_order_refund_can_retry(refund):
            raise ValidationError("当前退款单不需要重试。")
        audit_action = "order.after_sales.retry_refund"
    elif action == "approve":
        if case.status not in (
            ProviderOrderAfterSalesCase.Status.PENDING,
            ProviderOrderAfterSalesCase.Status.PROCESSING,
        ):
            raise ValidationError("仅待处理或处理中的售后单可以审核通过。")
        if approved_amount is None:
            raise ValidationError({"approved_amount": "请填写核准退款金额。"})
        if approved_amount <= 0:
            raise ValidationError({"approved_amount": "审核通过时核准退款金额必须大于 0。"})
        if approved_amount > case.requested_amount:
            raise ValidationError({"approved_amount": "核准金额不能超过申请金额。"})
        case.status = ProviderOrderAfterSalesCase.Status.APPROVED
        case.approved_amount = approved_amount
        case.result_note = result_note
        case.reviewed_by = actor
        case.reviewed_at = timezone.now()
        order.status = ProviderOrder.Status.AFTER_SALES
        order.save(update_fields=("status", "updated_at"))
        create_provider_order_refund(
            order_no=order.order_no,
            amount=approved_amount,
            source_type=ProviderOrderRefundOrder.SourceType.AFTER_SALES,
            source_reference=case.case_no,
            idempotency_key=f"provider-after-sales:{case.case_no}",
            reason=result_note,
            operator=actor,
        )
        audit_action = "order.after_sales.approve"
    else:
        if case.status not in (
            ProviderOrderAfterSalesCase.Status.PENDING,
            ProviderOrderAfterSalesCase.Status.PROCESSING,
        ):
            raise ValidationError("仅待处理或处理中的售后单可以驳回。")
        case.status = ProviderOrderAfterSalesCase.Status.REJECTED
        case.approved_amount = None
        case.result_note = result_note
        case.reviewed_by = actor
        case.reviewed_at = timezone.now()
        if order.status == ProviderOrder.Status.AFTER_SALES:
            order.status = case.original_order_status
            order.save(update_fields=("status", "updated_at"))
            if order.status == ProviderOrder.Status.PENDING_CONFIRMATION:
                reopen_provider_order_confirmation_timeout(order)
        settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
        if settlement and settlement.status == ProviderOrderSettlement.Status.DISPUTE_FROZEN:
            settlement.status = ProviderOrderSettlement.Status.RISK_FROZEN
            settlement.dispute_reason = ""
            settlement.save(update_fields=("status", "dispute_reason", "updated_at"))
            reopen_provider_order_settlement(settlement)
        audit_action = "order.after_sales.reject"
    case.save()
    AdminAuditLog.objects.create(
        actor=actor,
        organization=_organization(access),
        action=audit_action,
        target_type="provider_order_after_sales_case",
        target_id=case.case_no,
        before=before,
        after={
            "case_status": case.status,
            "order_status": order.status,
            "approved_amount": case.approved_amount,
            "result_note": case.result_note,
        },
        request_id=request.headers.get("X-Request-ID", ""),
        ip_address=client_ip(request),
    )
    if action in ("approve", "reject"):
        create_order_notification(
            order=order,
            event_type=UserNotification.EventType.ORDER_AFTER_SALES_RESULT,
            title="订单售后已同意" if action == "approve" else "订单售后已驳回",
            content=case.result_note or "订单售后已处理，请查看订单详情。",
            dedupe_suffix=f"{case.case_no}:{action}",
        )
    return case
