from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from .business_days import closure_deadline
from .models import AccountClosureRequest, User


def can_attempt_interactive_login(user: User) -> bool:
    # Pending users are deliberately inactive: JWT, refresh and Django sessions
    # cannot bypass the waiting period. Only verified login endpoints may restore them.
    return (
        user.account_status == User.AccountStatus.ACTIVE and user.is_active
    ) or (
        user.account_status == User.AccountStatus.CLOSURE_PENDING and not user.is_active
    )


def revoke_sessions(user: User) -> None:
    user.auth_version += 1
    user.save(update_fields=("auth_version",))
    for token in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=token)


def withdraw_closure_on_verified_login(user: User) -> bool:
    """Caller holds the user lock and has already verified password/SMS/WeChat ownership."""
    if user.account_status != User.AccountStatus.CLOSURE_PENDING:
        return False
    closure = AccountClosureRequest.objects.select_for_update().filter(
        user=user, status__in=(AccountClosureRequest.Status.PENDING, AccountClosureRequest.Status.BLOCKED),
    ).first()
    now = timezone.now()
    if closure is None or now >= closure.execute_after:
        raise ValidationError("账号注销等待期已结束，无法通过登录撤销，请联系客服。")
    closure.status = AccountClosureRequest.Status.CANCELLED
    closure.finished_at = now
    closure.save(update_fields=("status", "finished_at"))
    user.account_status = User.AccountStatus.ACTIVE
    user.is_active = True
    user.save(update_fields=("account_status", "is_active"))
    revoke_sessions(user)
    return True


@transaction.atomic
def request_account_closure(user: User, current_password: str) -> AccountClosureRequest:
    user = User.objects.select_for_update().get(pk=user.pk)
    if not user.is_active or user.account_status != User.AccountStatus.ACTIVE:
        raise ValidationError("当前账号不可申请注销，请联系客服。")
    if not user.check_password(current_password):
        raise ValidationError({"current_password": "当前密码不正确。"})
    blockers = account_closure_blockers(user)
    if blockers:
        summary = "、".join(f"{item['label']} {item['count']} 项" for item in blockers[:4])
        if len(blockers) > 4:
            summary += f"等 {len(blockers)} 类"
        raise ValidationError({
            "business": f"账号还有未结业务：{summary}。请处理完成后再注销。",
            "blocking_items": blockers,
        })
    if user.closure_requests.filter(status__in=("pending", "blocked")).exists():
        raise ValidationError("已有待处理注销申请，请联系客服。")
    now = timezone.now()
    closure = AccountClosureRequest.objects.create(
        user=user, requested_at=now, execute_after=closure_deadline(now),
    )
    user.account_status = User.AccountStatus.CLOSURE_PENDING
    user.is_active = False
    user.save(update_fields=("account_status", "is_active"))
    revoke_sessions(user)
    return closure


@transaction.atomic
def process_account_closure(user_id: int, *, now=None) -> str:
    """User -> request lock order matches login/submission. Safe for concurrent workers."""
    now = now or timezone.now()
    user = User.objects.select_for_update(skip_locked=True).filter(pk=user_id).first()
    if user is None:
        return "skipped"
    closure = AccountClosureRequest.objects.select_for_update().filter(
        user=user, status__in=("pending", "blocked"), execute_after__lte=now,
    ).first()
    if closure is None or user.account_status != User.AccountStatus.CLOSURE_PENDING:
        return "skipped"
    blockers = account_closure_blockers(user)
    closure.checked_at = now
    closure.blocking_items = blockers
    if blockers:
        closure.status = AccountClosureRequest.Status.BLOCKED
        closure.save(update_fields=("checked_at", "blocking_items", "status"))
        return "blocked"
    user.account_status = User.AccountStatus.CLOSED
    user.is_active = False
    user.save(update_fields=("account_status", "is_active"))
    revoke_sessions(user)
    # These are optional, unsubmitted receiving materials, not financial records.
    from providers.models import ProviderReceivingAccount
    ProviderReceivingAccount.objects.filter(provider__user=user).delete()
    closure.status = AccountClosureRequest.Status.COMPLETED
    closure.finished_at = now
    closure.save(update_fields=("checked_at", "blocking_items", "status", "finished_at"))
    return "completed"


def lock_active_user_for_business(user) -> User:
    """Serialize account closure with creation of new chargeable business."""

    locked_user = User.objects.select_for_update().get(pk=user.pk)
    if not locked_user.is_active or locked_user.account_status != User.AccountStatus.ACTIVE:
        raise PermissionDenied("当前账号状态不可发起新的业务。")
    return locked_user


def account_closure_blockers(user) -> list[dict[str, object]]:
    """Return unresolved obligations that require the account to remain operable."""

    from activities.models import (
        Activity,
        ActivityAfterSalesCase,
        ActivityParticipation,
        ActivityParticipationPaymentOrder,
        ActivityParticipationRefundOrder,
        ActivityPublishOrder,
        ActivitySettlement,
    )
    from backoffice.models import ProviderOrderAfterSalesCase
    from orders.models import (
        ProviderOrder,
        ProviderOrderPaymentOrder,
        ProviderOrderRefundOrder,
        ProviderOrderSettlement,
    )
    from supportcases.models import SupportCase
    from wallets.models import UserWallet, WalletRechargeOrder

    checks = (
        (
            "wallet_balance", "未结清钱包余额",
            UserWallet.objects.filter(user=user).filter(
                Q(available_balance__gt=0) | Q(frozen_balance__gt=0)
            ),
        ),
        (
            "wallet_recharges", "待核对充值单",
            WalletRechargeOrder.objects.filter(
                user=user, status=WalletRechargeOrder.Status.PENDING_PAYMENT,
            ),
        ),
        (
            "provider_orders",
            "待履约陪玩订单",
            ProviderOrder.objects.filter(
                Q(customer=user) | Q(provider__user=user)
            ).exclude(
                status__in=(
                    ProviderOrder.Status.COMPLETED,
                    ProviderOrder.Status.CANCELLED,
                    ProviderOrder.Status.REFUNDED,
                )
            ),
        ),
        (
            "provider_payments",
            "待核对陪玩支付",
            ProviderOrderPaymentOrder.objects.filter(
                payer=user,
                status=ProviderOrderPaymentOrder.Status.PENDING_PAYMENT,
            ),
        ),
        (
            "provider_refunds",
            "未完成陪玩退款",
            ProviderOrderRefundOrder.objects.filter(beneficiary=user).exclude(
                status=ProviderOrderRefundOrder.Status.SUCCEEDED
            ),
        ),
        (
            "provider_after_sales",
            "处理中陪玩售后",
            ProviderOrderAfterSalesCase.objects.filter(
                Q(creator=user)
                | Q(order__customer=user)
                | Q(order__provider__user=user),
                status__in=(
                    ProviderOrderAfterSalesCase.Status.PENDING,
                    ProviderOrderAfterSalesCase.Status.PROCESSING,
                    ProviderOrderAfterSalesCase.Status.APPROVED,
                ),
            ).distinct(),
        ),
        (
            "provider_settlements",
            "未完成达人结算",
            ProviderOrderSettlement.objects.filter(provider__user=user).exclude(
                status__in=(
                    ProviderOrderSettlement.Status.SETTLED,
                    ProviderOrderSettlement.Status.CANCELLED,
                )
            ),
        ),
        (
            "organized_activities",
            "进行中的发起活动",
            Activity.objects.filter(
                organizer=user,
                status__in=(
                    Activity.Status.PENDING_REVIEW,
                    Activity.Status.RECRUITING,
                    Activity.Status.FORMED,
                    Activity.Status.IN_PROGRESS,
                ),
            ),
        ),
        (
            "activity_participations",
            "未结束活动报名",
            ActivityParticipation.objects.filter(
                user=user,
                status__in=(
                    ActivityParticipation.Status.PENDING_PAYMENT,
                    ActivityParticipation.Status.ACTIVE,
                ),
                activity__status__in=(
                    Activity.Status.PENDING_REVIEW,
                    Activity.Status.RECRUITING,
                    Activity.Status.FORMED,
                    Activity.Status.IN_PROGRESS,
                ),
            ),
        ),
        (
            "activity_payments",
            "待支付活动订单",
            ActivityParticipationPaymentOrder.objects.filter(
                payer=user,
                status=ActivityParticipationPaymentOrder.Status.PENDING_PAYMENT,
            ),
        ),
        (
            "activity_publish_payments",
            "待支付活动发布单",
            ActivityPublishOrder.objects.filter(
                payer=user,
                status=ActivityPublishOrder.Status.PENDING_PAYMENT,
            ),
        ),
        (
            "activity_refunds",
            "未完成活动退款",
            ActivityParticipationRefundOrder.objects.filter(
                Q(beneficiary=user) | Q(activity__organizer=user)
            ).exclude(status=ActivityParticipationRefundOrder.Status.SUCCEEDED),
        ),
        (
            "activity_after_sales",
            "处理中活动售后",
            ActivityAfterSalesCase.objects.filter(
                Q(applicant=user) | Q(participation__activity__organizer=user),
                status__in=(
                    ActivityAfterSalesCase.Status.PENDING,
                    ActivityAfterSalesCase.Status.PROCESSING,
                    ActivityAfterSalesCase.Status.APPROVED,
                ),
            ).distinct(),
        ),
        (
            "activity_settlements",
            "未完成活动结算",
            ActivitySettlement.objects.filter(beneficiary=user).exclude(
                status=ActivitySettlement.Status.SETTLED
            ),
        ),
        (
            "support_cases",
            "处理中客服工单",
            SupportCase.objects.filter(
                reporter=user,
                status__in=(
                    SupportCase.Status.PENDING,
                    SupportCase.Status.PROCESSING,
                    SupportCase.Status.REVIEWING,
                ),
            ),
        ),
    )
    blockers = []
    from providers.models import ProviderReceivingAccount
    if ProviderReceivingAccount.objects.filter(provider__user=user, attempts__isnull=False).exists():
        blockers.append({"code": "receiving_channel", "label": "需客服核对的渠道收款账户", "count": 1})
    for code, label, queryset in checks:
        count = queryset.count()
        if count:
            blockers.append({"code": code, "label": label, "count": count})
    return blockers
