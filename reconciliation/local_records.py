"""Read-only projection. Amounts are channel portions, not mixed-payment totals."""
from django.db.models import Q, Min

from activities.models import ActivityHuifuPaymentOrder, ActivityHuifuRefundOrder
from orders.distributions import _scope
from orders.models import ProviderOrderPaymentOrder, ProviderOrderRefundOrder, ProviderOrderDistribution
from providers.models import ProviderWithdrawal
from wallets.models import WalletRechargeOrder, WalletPaymentAllocation
from .gateway import BillError, MAX_ROWS


def records_for(config, day, bill_type, keys):
    dates, sequences = {k[0] for k in keys}, {k[1] for k in keys}
    seen_request = Q(req_date__in=dates, req_seq_id__in=sequences)
    records = []

    def add(obj, kind, reference, amount, status, happened, trade="", fee=None, recipients=None, original="", route=""):
        if len(records) >= MAX_ROWS:
            raise BillError("local_limit")
        records.append({
            "key": (obj.req_date, obj.req_seq_id), "kind": kind, "reference": reference,
            "amount": amount, "status": status, "trade": trade, "fee": fee,
            "recipients": recipients, "original": original, "route": route,
            "expected": status == "成功" and happened is not None and happened.date() == day,
        })

    def status(value):
        return {"paid": "成功", "partially_refunded": "成功", "refunded": "成功", "succeeded": "成功",
                "failed": "失败", "S": "成功", "F": "失败"}.get(value, "待核实")

    def external(business_type, order, fallback):
        allocation = WalletPaymentAllocation.objects.filter(
            business_type=business_type, business_order_no=order.order_no,
        ).first()
        return allocation.external_amount if allocation else fallback

    def at_day(value):
        from django.utils import timezone
        return timezone.localtime(value) if value else None

    if bill_type == "TRADE_BILL":
        for p in ProviderOrderPaymentOrder.objects.filter(
            Q(paid_at__date=day) | seen_request, gateway_merchant_id=config.merchant_id,
        ).exclude(req_seq_id="").select_related("order").iterator():
            amount = external("provider_order", p.order, p.payable_amount)
            if amount:
                add(p, "payment", p.payment_no, amount, status(p.status), at_day(p.paid_at), p.gateway_trade_no)
        for p in WalletRechargeOrder.objects.filter(
            Q(paid_at__date=day) | seen_request, gateway_merchant_id=config.merchant_id,
        ).exclude(req_seq_id="").iterator():
            add(p, "payment", p.order_no, p.payable_amount, status(p.status), at_day(p.paid_at), p.gateway_trade_no)
        for p in ActivityHuifuPaymentOrder.objects.filter(
            Q(publish_order__paid_at__date=day) | Q(participation_order__paid_at__date=day) | seen_request,
            gateway_merchant_id=config.merchant_id,
        ).exclude(req_seq_id="").select_related("publish_order", "participation_order").iterator():
            order = p.publish_order or p.participation_order
            kind = "activity_publish" if p.publish_order_id else "activity_participation"
            amount = external(kind, order, order.payable_amount)
            if amount:
                add(p, "payment", order.order_no, amount, status(order.status), at_day(order.paid_at), p.gateway_trade_no)
        for r in ProviderOrderRefundOrder.objects.filter(
            Q(refunded_at__date=day) | seen_request, gateway_merchant_id=config.merchant_id, external_refund_amount__gt=0,
        ).exclude(req_seq_id="").select_related("payment_order").iterator():
            add(r, "refund", r.refund_no, r.external_refund_amount, status(r.status), at_day(r.refunded_at),
                r.gateway_refund_no, original=r.payment_order.gateway_trade_no)
        for r in ActivityHuifuRefundOrder.objects.filter(
            Q(publish_refund__refunded_at__date=day) | Q(participation_refund__refunded_at__date=day) | seen_request,
            gateway_merchant_id=config.merchant_id,
        ).exclude(req_seq_id="").select_related("publish_refund", "participation_refund").iterator():
            refund = r.publish_refund or r.participation_refund
            if refund.external_refund_amount:
                add(r, "refund", refund.refund_no, refund.external_refund_amount, status(refund.status),
                    at_day(refund.refunded_at), r.gateway_refund_no)
    if bill_type in ("TRADE_BILL", "SPLIT_BILL"):
        distributions = ProviderOrderDistribution.objects.filter(snapshot__scope=_scope(config)).annotate(
            first_success=Min("observations__created_at", filter=Q(observations__status="succeeded")),
        ).filter(Q(first_success__date=day) | seen_request).select_related("settlement")
        for d in distributions.iterator():
            snap = d.snapshot
            if bill_type == "TRADE_BILL":
                if d.route != "balance":
                    continue
                add(d, "balance", d.settlement.settlement_no, snap["provider_amount"], status(d.status),
                    at_day(d.first_success), d.gateway_trade_no, d.account_payment_fee_amount)
            else:
                receivers = {snap["receiver_id"]: snap["provider_amount"]}
                if d.route == "delayed":
                    platform = snap.get("platform_split_amount", snap["platform_amount"])
                    if platform:
                        receivers[config.merchant_id] = platform
                add(d, "split", d.settlement.settlement_no, sum(receivers.values()), status(d.status),
                    at_day(d.first_success), fee=d.split_fee_amount if d.route == "delayed" else d.account_payment_fee_amount,
                    recipients=receivers,
                    original=d.gateway_trade_no if d.route == "balance" else snap.get("payment", {}).get("gateway_trade_no", ""),
                    route=d.route)
    if bill_type == "WITHDRAWAL_BILL":
        # Official outcash template covers disbursement/e-account withdrawal.
        # Do not infer that it also contains personal-user manual withdrawals.
        for w in ProviderWithdrawal.objects.filter(
            snapshot__scope=_scope(config),
        ).filter(Q(created_at__date=day) | seen_request).iterator():
            add(w, "withdrawal", w.req_seq_id, w.amount, status(w.status), None, w.gateway_trade_no, w.fee_amount)
    return records
