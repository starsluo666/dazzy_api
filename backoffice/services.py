from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from orders.models import ProviderOrder
from providers.models import ProviderLiveLocation, ProviderProfile

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
def review_provider_application(*, profile_id, decision, reason, actor, access, request):
    queryset = ProviderProfile.objects.select_for_update().select_related("user")
    if not access.all_data:
        queryset = queryset.filter(service_city_code__in=access.city_codes)
    profile = get_object_or_404(queryset, id=profile_id)
    if profile.status != ProviderProfile.Status.PENDING:
        raise ValidationError("仅待审核申请可以执行审核。")
    if (
        decision == "approve"
        and profile.user.verification_status != profile.user.VerificationStatus.VERIFIED
    ):
        raise ValidationError({"decision": "申请人尚未完成实名认证，不能通过达人审核。"})
    if decision == "approve" and not profile.lifestyle_photo_id:
        raise ValidationError({"decision": "申请人尚未上传生活照，不能通过达人审核。"})
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
    return profile


def _scoped_provider_orders(access):
    queryset = ProviderOrder.objects.all()
    if not access.all_data:
        queryset = queryset.filter(provider__service_city_code__in=access.city_codes)
    return queryset


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
    elif action == "approve":
        if case.status not in (
            ProviderOrderAfterSalesCase.Status.PENDING,
            ProviderOrderAfterSalesCase.Status.PROCESSING,
        ):
            raise ValidationError("仅待处理或处理中的售后单可以审核通过。")
        if approved_amount is None:
            raise ValidationError({"approved_amount": "请填写核准退款金额。"})
        if case.case_type == ProviderOrderAfterSalesCase.CaseType.REFUND and approved_amount <= 0:
            raise ValidationError({"approved_amount": "退款申请的核准金额必须大于 0。"})
        if approved_amount > case.requested_amount:
            raise ValidationError({"approved_amount": "核准金额不能超过申请金额。"})
        case.status = ProviderOrderAfterSalesCase.Status.APPROVED
        case.approved_amount = approved_amount
        case.result_note = result_note
        case.reviewed_by = actor
        case.reviewed_at = timezone.now()
        order.status = ProviderOrder.Status.AFTER_SALES
        order.save(update_fields=("status", "updated_at"))
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
    return case
