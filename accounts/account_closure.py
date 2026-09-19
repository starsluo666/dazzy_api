from django.db.models import Q
from rest_framework.exceptions import PermissionDenied

from .models import User


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

    checks = (
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
    for code, label, queryset in checks:
        count = queryset.count()
        if count:
            blockers.append({"code": code, "label": label, "count": count})
    return blockers
