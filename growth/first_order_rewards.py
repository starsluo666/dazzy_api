from django.utils import timezone

from .models import ProviderInviteAttribution, ProviderInviteReward


def eligible_first_order(settlement, *, now=None):
    from backoffice.models import ProviderOrderAfterSalesCase
    from orders.models import ProviderOrder, ProviderOrderRefundOrder

    order = settlement.order
    return (
        settlement.status == settlement.Status.SETTLED
        and settlement.freeze_until <= (now or timezone.now())
        and order.status in (ProviderOrder.Status.PENDING_REVIEW, ProviderOrder.Status.COMPLETED)
        and order.paid_at is not None
        and bool(order.customer_confirmed_at or order.auto_confirmed_at)
        and not order.fulfillment_review_required
        and not order.cancellation_record
        and settlement.paid_amount > 0
        and not settlement.refunded_amount
        and not order.refund_orders.filter(status__in=(
            ProviderOrderRefundOrder.Status.PENDING,
            ProviderOrderRefundOrder.Status.PROCESSING,
            ProviderOrderRefundOrder.Status.SUCCEEDED,
        )).exists()
        and not ProviderOrderAfterSalesCase.objects.filter(
            order=order, status__in=("pending", "processing", "approved"),
        ).exists()
    )


def record_first_order_reward(settlement, *, now=None):
    """Called inside the settlement transaction while the order is locked."""
    if settlement.order.renewal_of_id:
        return None
    attribution = ProviderInviteAttribution.objects.select_for_update().filter(
        profile_id=settlement.provider_id, first_order__isnull=True,
    ).first()
    if not attribution or not attribution.first_order_rule.get("enabled"):
        return
    if not eligible_first_order(settlement, now=now):
        return
    rule = attribution.first_order_rule
    base = settlement.net_service_fee_amount
    amount = rule["amount"] if rule["mode"] == "fixed" else (base * rule["rate_bps"] + 5000) // 10000
    attribution.first_order_id = settlement.order_id
    attribution.save(update_fields=("first_order",))
    if amount:
        ProviderInviteReward.objects.create(
            source_id=attribution.source_id, profile_id=settlement.provider_id,
            invitee_id=settlement.provider.user_id,
            kind=ProviderInviteReward.Kind.FIRST_ORDER, order_id=settlement.order_id,
            payout_method=ProviderInviteReward.PayoutMethod.BALANCE,
            amount=amount, calculation={**rule, "net_service_fee_amount": base},
            code_snapshot=attribution.code_snapshot,
            source_name_snapshot=attribution.source_name_snapshot,
            config_revision=attribution.config_revision,
        )
