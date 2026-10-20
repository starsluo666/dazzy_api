"""Local funding inventory, NOT a Huifu balance-payment implementation.

The account-payment executor is separate and default-closed. Nothing here
creates a channel request, reserves channel funds or credits income.
Snapshots are saved by the existing order-locked, revisioned settlement plan.
"""

import hashlib
import json

from django.conf import settings

from wallets.lots import CONSUMPTION_PRICING_VERSION
from wallets.models import WalletPaymentAllocation, WalletRechargeOrder


MAX_SOURCE_ROWS = 100
PENDING_CONFIRMATIONS = (
    "确认平台商户是否获准向达人个人收款账户进行余额支付",
    "确认余额支付的实际业务类型、手续费扣收模式及风控资料来源",
    "核实充值与外部支付资金所在账户、可用金额及资金使用周期",
    "完成真实小额划款、到账核验及重复请求验收",
)


def _evidence_digest(payment):
    # Track changes without exposing merchant IDs, raw channel payloads or keys.
    evidence = [
        payment.gateway_merchant_id, payment.req_date, payment.req_seq_id,
        payment.gateway_trade_no, payment.gateway_last_query_status,
        payment.gateway_last_query_digest,
    ]
    return hashlib.sha256(json.dumps(evidence, separators=(",", ":")).encode()).hexdigest()


def _has_payment_record(payment, merchant_id):
    return bool(
        merchant_id and payment.gateway_merchant_id == merchant_id
        and payment.req_date and payment.req_seq_id and payment.gateway_trade_no
        and payment.paid_at and payment.gateway_last_query_status == "S"
        and payment.gateway_last_query_digest
    )


def build_wallet_settlement_preparation(*, order, settlement, funding, blockers, cancelled):
    """All amounts are cents; do not allocate an unconfirmed channel funding route.

    Caller holds the order lock shared by wallet consumption/refund operations.
    The historical query digest is only local evidence, never current funds proof.
    No current wallet/lot balance enters this snapshot: spending on another order
    must not rewrite this order's original funding or create spurious revisions.
    """
    allocation = WalletPaymentAllocation.objects.filter(
        business_type=WalletPaymentAllocation.BusinessType.PROVIDER_ORDER,
        business_order_no=order.order_no,
    ).first()
    if not allocation or not allocation.wallet_amount:
        return None

    issues = []

    def issue(code, message):
        if not any(item["code"] == code for item in issues):
            issues.append({"code": code, "message": message})

    local_valid = funding["locally_reconciled"]
    wallet_remaining = (
        funding["wallet_paid_amount"] - funding["wallet_refunded_amount"]
        if local_valid else None
    )
    external_remaining = (
        funding["external_paid_amount"] - funding["external_refunded_amount"]
        if local_valid else None
    )
    if not local_valid:
        issue("allocation_mismatch", "订单支付、退款与余额扣款记录不一致，须先核账")
    merchant_id = settings.HUIFU_MERCHANT_ID.strip()
    if not merchant_id:
        issue("merchant_not_configured", "尚未配置用于比对资金归属的平台商户")

    rows = list(allocation.lot_debits.select_related(
        "lot__wallet", "lot__recharge_order",
    ).order_by("id")[:MAX_SOURCE_ROWS + 1])
    truncated = len(rows) > MAX_SOURCE_ROWS
    if truncated:
        issue("source_limit", "充值来源超过单次核对上限，须人工核账；当前明细不完整")
    sources = []
    debit_total = restored_total = traceable_total = 0
    for debit in rows[:MAX_SOURCE_ROWS]:
        lot, recharge = debit.lot, debit.lot.recharge_order
        debit_total += debit.amount
        restored_total += debit.restored_amount
        remaining = debit.amount - debit.restored_amount
        owner_matches = (
            allocation.user_id == order.customer_id
            and lot.wallet.user_id == order.customer_id
            and recharge.user_id == order.customer_id
        )
        amount_matches = (
            isinstance(recharge.pricing_snapshot, dict)
            and recharge.pricing_snapshot.get("version") == CONSUMPTION_PRICING_VERSION
            and 0 < debit.amount <= lot.credited_amount == recharge.credited_amount
            == recharge.payable_amount
            and 0 <= debit.restored_amount <= debit.amount
            and lot.discount_rate_bps == recharge.discount_rate_bps
        )
        record_complete = bool(
            owner_matches and amount_matches
            and recharge.status == WalletRechargeOrder.Status.PAID
            and _has_payment_record(recharge, merchant_id)
        )
        if not owner_matches:
            issue("source_owner_mismatch", "充值来源与下单用户不一致，须人工核账")
        if not amount_matches:
            issue("source_amount_mismatch", "充值批次、入账金额或退款记录不一致，须人工核账")
        if not record_complete:
            issue("recharge_record_unverified", "部分充值缺少一致的原支付及成功查询记录，须核对原流水")
        else:
            traceable_total += remaining
        sources.append({
            "recharge_no": recharge.order_no if owner_matches else "",
            "debited_amount": debit.amount,
            "restored_amount": debit.restored_amount,
            "remaining_amount": remaining,
            "record_complete": record_complete,
            "evidence_digest": _evidence_digest(recharge) if owner_matches else "",
        })

    totals_valid = bool(
        local_valid and not truncated
        and 0 <= debit_total <= allocation.wallet_amount
        and 0 <= restored_total <= allocation.wallet_refunded_amount
        and allocation.wallet_refunded_amount - restored_total
        <= allocation.wallet_amount - debit_total
    )
    if not totals_valid:
        issue("lot_totals_unverified", "余额批次扣款及退回总额未核对完整")
    untraced = wallet_remaining - traceable_total if totals_valid else None
    if untraced:
        issue("untraced_wallet_funds", "存在无法追溯到完整充值记录的余额，不能自动使用平台资金补付")

    payment = getattr(order, "payment_order", None)
    external = None
    if allocation.external_amount:
        external_complete = bool(
            local_valid and payment and payment.channel in ("wechat", "alipay")
            and _has_payment_record(payment, merchant_id)
        )
        if not external_complete:
            issue("external_record_unverified", "组合支付的外部付款记录或收款商户尚未核实")
        external = {
            "payment_no": payment.payment_no if payment else "",
            "paid_amount": funding["external_paid_amount"],
            "refunded_amount": funding["external_refunded_amount"],
            "remaining_amount": external_remaining,
            "delay_acct_flag": payment.delay_acct_flag if payment else "",
            "record_complete": external_complete,
            "evidence_digest": _evidence_digest(payment) if payment else "",
        }
        if payment and payment.delay_acct_flag != "N":
            issue("external_route_review", "外部付款不是普通即时入账交易，须确认资金位置，不能直接按商户余额划付")

    business_blockers = [item for item in blockers if item["code"] != "wallet_route_unconfirmed"]
    if settlement.refunded_amount and not cancelled:
        business_blockers.append({
            "code": "refunded_order",
            "message": "部分退款后的剩余款仍需单独核账，本准备单不放行出款",
        })
    status = "not_required" if cancelled else (
        "blocked" if issues or business_blockers else "awaiting_channel"
    )
    return {
        "version": "provider-wallet-preparation-v1",
        "status": status,
        "status_label": {
            "not_required": "无需结算",
            "blocked": "待业务与资金核对",
            "awaiting_channel": "待渠道资金核验",
        }[status],
        # Preparation is read-only; this is not the separate executor's switch.
        "execution_enabled": False,
        "channel_funds_verified": False,
        "channel_transfer_amount": None,
        "channel_fee_amount": None,
        "wallet_remaining_amount": wallet_remaining,
        "external_remaining_amount": external_remaining,
        "traceable_wallet_amount": traceable_total if totals_valid else None,
        "untraced_wallet_amount": untraced,
        "provider_receivable_amount": settlement.provider_settlement_amount,
        "platform_gross_amount": settlement.platform_commission_amount,
        "sources": sources,
        "sources_truncated": truncated,
        "external_payment": external,
        "source_issues": issues,
        "business_blockers": business_blockers,
        "pending_confirmations": [] if cancelled else list(PENDING_CONFIRMATIONS),
    }
