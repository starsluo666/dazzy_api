"""Prepare auditable distribution records; deliberately no SDK or transfer entry point.

Local wallet debits are NOT Huifu account payments. Funding verification here only
reconciles local records. It never establishes channel funds availability.
"""

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from backoffice.models import ProviderOrderAfterSalesCase
from wallets.models import WalletPaymentAllocation

from .models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderSettlement,
    ProviderOrderSettlementPlan,
    ProviderOrderSettlementPlanRevision,
)
from .settlement_fees import platform_fee_snapshot


OPEN_AFTER_SALES_STATUSES = (
    ProviderOrderAfterSalesCase.Status.PENDING,
    ProviderOrderAfterSalesCase.Status.PROCESSING,
    ProviderOrderAfterSalesCase.Status.APPROVED,
)


def settlement_has_unresolved_refunds(order):
    # FAILED is still an unresolved liability, not permission to pay the provider.
    return order.refund_orders.exclude(status=ProviderOrderRefundOrder.Status.SUCCEEDED).exists()


def _funding_snapshot(order, settlement):
    payment = ProviderOrderPaymentOrder.objects.filter(order=order).first()
    allocation = WalletPaymentAllocation.objects.filter(
        business_type=WalletPaymentAllocation.BusinessType.PROVIDER_ORDER,
        business_order_no=order.order_no,
    ).first()
    refunds = order.refund_orders.filter(
        status=ProviderOrderRefundOrder.Status.SUCCEEDED,
    ).aggregate(
        wallet=Sum("wallet_refund_amount"),
        external=Sum("external_refund_amount"),
        total=Sum("refund_amount"),
    )
    payment_valid = bool(
        payment
        and payment.payer_id == order.customer_id
        and payment.payable_amount == order.payable_amount == settlement.paid_amount
        and payment.status
        in (
            ProviderOrderPaymentOrder.Status.PAID,
            ProviderOrderPaymentOrder.Status.PARTIALLY_REFUNDED,
            ProviderOrderPaymentOrder.Status.REFUNDED,
        )
    )
    verified = bool(
        payment_valid
        and allocation
        and allocation.user_id == order.customer_id
        and allocation.payable_amount == settlement.paid_amount
        and allocation.wallet_amount + allocation.external_amount == settlement.paid_amount
        and not allocation.wallet_released
        and allocation.status
        in (
            WalletPaymentAllocation.Status.CONSUMED,
            WalletPaymentAllocation.Status.PARTIALLY_REFUNDED,
            WalletPaymentAllocation.Status.REFUNDED,
        )
        and allocation.wallet_refunded_amount == (refunds["wallet"] or 0)
        and allocation.external_refunded_amount == (refunds["external"] or 0)
        and (refunds["total"] or 0) == settlement.refunded_amount
        and allocation.wallet_refunded_amount <= allocation.wallet_amount
        and allocation.external_refunded_amount <= allocation.external_amount
    )
    funding_type = ProviderOrderSettlementPlan.FundingType.UNKNOWN
    if verified:
        funding_type = (
            ProviderOrderSettlementPlan.FundingType.MIXED
            if allocation.wallet_amount and allocation.external_amount
            else ProviderOrderSettlementPlan.FundingType.WALLET
            if allocation.wallet_amount
            else ProviderOrderSettlementPlan.FundingType.EXTERNAL
        )
    # Unknown is null, never an invented zero or an inferred legacy external payment.
    snapshot = {
        "version": "provider-funding-v1",
        "locally_reconciled": verified,
        "channel_funds_verified": False,
        "payment_valid": payment_valid,
        "payment_no": payment.payment_no if payment else "",
        "payment_channel": payment.channel if payment else "",
        "payment_status": payment.status if payment else "",
        "payment_req_date": payment.req_date if payment else "",
        "payment_req_seq_id": payment.req_seq_id if payment else "",
        "payment_gateway_trade_no": payment.gateway_trade_no if payment else "",
        "allocation_id": allocation.pk if allocation else None,
        "wallet_paid_amount": allocation.wallet_amount if verified else None,
        "external_paid_amount": allocation.external_amount if verified else None,
        "wallet_refunded_amount": allocation.wallet_refunded_amount if verified else None,
        "external_refunded_amount": allocation.external_refunded_amount if verified else None,
    }
    return funding_type, snapshot


@transaction.atomic
def sync_provider_settlement_plan(*, order_no, now=None, new_settlement=False):
    """Order row serializes all writers, including refund and completion handlers.

    Only the settlement-creation hook passes new_settlement=True. Existing records
    are never silently approved for historical payouts. No external calls occur.
    """
    now = now or timezone.now()
    order = ProviderOrder.objects.select_for_update().get(order_no=order_no)
    settlement = ProviderOrderSettlement.objects.select_for_update().filter(order=order).first()
    if not settlement:
        return None
    plan = (
        ProviderOrderSettlementPlan.objects.select_for_update()
        .filter(
            settlement=settlement,
        )
        .first()
    )
    manual_review = plan.requires_manual_review if plan else not new_settlement
    funding_type, funding = _funding_snapshot(order, settlement)
    blockers = []

    def block(code, message):
        blockers.append({"code": code, "message": message})

    cancelled = (
        settlement.status == ProviderOrderSettlement.Status.CANCELLED
        or settlement.paid_amount == settlement.refunded_amount
    )
    waiting = False
    if not cancelled:
        if order.status == ProviderOrder.Status.TERMINATED:
            block("termination_reconciliation", "提前终止剩余款待核账，暂不自动分账")
            waiting = True
        if order.fulfillment_review_required:
            block("fulfillment_review", "履约时间异常，等待客服审核")
            waiting = True
        if not (order.customer_confirmed_at or order.auto_confirmed_at) or order.status not in (
            ProviderOrder.Status.PENDING_REVIEW,
            ProviderOrder.Status.COMPLETED,
            ProviderOrder.Status.AFTER_SALES,
        ):
            block("order_not_completed", "订单尚未确认完成或状态需核实")
            waiting = True
        if now < settlement.freeze_until:
            block("freeze_period", "尚未到结算冻结期截止时间")
            waiting = True
        if order.after_sales_cases.filter(status__in=OPEN_AFTER_SALES_STATUSES).exists():
            block("after_sales_open", "存在待处理售后")
            waiting = True
        if settlement_has_unresolved_refunds(order):
            block("refund_unresolved", "存在未完成或失败待处理的退款")
            waiting = True
        if settlement.status == ProviderOrderSettlement.Status.DISPUTE_FROZEN:
            block("dispute_frozen", "结算处于争议冻结状态")
            waiting = True
        if settlement.status != ProviderOrderSettlement.Status.SETTLED:
            block("ledger_not_settled", "平台账务尚未完成结算")
            waiting = True
        if manual_review:
            block("historical_review", "历史结算须核对渠道流水、退款及既有出款记录")
        if not funding["locally_reconciled"]:
            block("funding_unverified", "支付来源或退款记录不完整、不一致，需核账")
        if funding_type in (
            ProviderOrderSettlementPlan.FundingType.WALLET,
            ProviderOrderSettlementPlan.FundingType.MIXED,
        ):
            block("wallet_route_unconfirmed", "余额部分尚未接通渠道资金划转")
        # This is local readiness, NOT permission/proof of channel execution.
        # Dispatch owns cohort/flags/receiver/funds/fee checks; channel results
        # live on the distribution record, never as unconditional plan blockers.

    values = {
        "status": ProviderOrderSettlementPlan.Status.CANCELLED
        if cancelled
        else (
            ProviderOrderSettlementPlan.Status.WAITING
            if waiting
            else ProviderOrderSettlementPlan.Status.BLOCKED if blockers
            else ProviderOrderSettlementPlan.Status.READY
        ),
        "funding_type": funding_type,
        "paid_amount": settlement.paid_amount,
        "refunded_amount": settlement.refunded_amount,
        "provider_amount": settlement.provider_settlement_amount,
        "platform_amount": settlement.platform_commission_amount,
        "funding_snapshot": funding,
        # Business policy is not proof of channel configuration. Actual channel
        # fees belong to distribution/withdrawal records, not this local plan.
        "fee_policy_snapshot": platform_fee_snapshot(
            provider_amount=settlement.provider_settlement_amount,
            platform_amount=settlement.platform_commission_amount,
        ),
        "blockers": blockers,
        "requires_manual_review": manual_review,
    }
    # Include accounting inputs in the audit even when the resulting totals match.
    snapshot = {
        **values,
        "order_no": order.order_no,
        "settlement_no": settlement.settlement_no,
        "settlement_status": settlement.status,
        "freeze_until": settlement.freeze_until.isoformat(),
        "commission_rate": str(settlement.platform_commission_rate),
        "calculation": settlement.calculation_snapshot,
    }
    if not plan:
        plan = ProviderOrderSettlementPlan.objects.create(
            settlement=settlement,
            evaluated_at=now,
            **values,
        )
        changed = True
    else:
        previous = plan.revisions.order_by("-revision").first()
        changed = not previous or previous.snapshot != snapshot
        for key, value in values.items():
            setattr(plan, key, value)
        if changed:
            plan.revision += 1
        plan.evaluated_at = now
        plan.save()
    if changed:
        ProviderOrderSettlementPlanRevision.objects.create(
            plan=plan,
            revision=plan.revision,
            snapshot=snapshot,
        )
    return plan
