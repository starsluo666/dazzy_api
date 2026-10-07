"""Read-only funding diagnostics. No SDK calls, retries, balance writes or reconciliation fixes."""
from datetime import timedelta

from django.db.models import BigIntegerField, Case, CharField, Exists, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Concat, MD5

from orders.models import ProviderOrderDistribution, ProviderOrderDistributionPreflight
from providers.models import ProviderIncomeEntry, ProviderIncomeWallet, ProviderWithdrawal


FINANCE_QUEUES = (
    "distribution_attention", "distribution_preflight_attention", "distribution_credit_attention",
    "withdrawal_attention", "income_reconciliation",
)


def wallet_balances(queryset):
    entries = ProviderIncomeEntry.objects.filter(wallet_id=OuterRef("pk")).order_by().values("wallet_id")
    for name, field, kind in (("available", "available_delta", None), ("reserved", "reserved_delta", None), ("paid", "amount", "paid")):
        source = entries.filter(kind=kind) if kind else entries
        total = source.annotate(total=Sum(field)).values("total")[:1]
        queryset = queryset.annotate(**{f"_expected_{name}": Coalesce(Subquery(total), Value(0), output_field=BigIntegerField())})
    return queryset.annotate(_mismatch=Case(When(
        Q(available_amount=F("_expected_available")) & Q(reserved_amount=F("_expected_reserved")) & Q(paid_amount=F("_expected_paid")),
        then=Value(False)), default=Value(True)))


def finance_queues(providers, now, Queue):
    splits = ProviderOrderDistribution.objects.filter(settlement__provider__in=providers)
    flagged = splits.filter(
        Q(status__in=("failed", "unknown")) | ~Q(evidence_conflict_code="") | ~Q(attention_reason="")
        | Q(status__in=("submitting", "processing"), created_at__lte=now - timedelta(minutes=30))
    )
    # Query timestamps and repeated observations must not generate a new alert every minute.
    split_version = MD5(Concat("status", Value(":"), "evidence_conflict_code", Value(":"), "attention_reason"))
    preflight = ProviderOrderDistributionPreflight.objects.filter(
        settlement__provider__in=providers, settlement__distribution__isnull=True,
    ).exclude(settlement__status="cancelled").filter(
        Q(status="blocked") | (Q(status="deferred", failure_count__gte=3)
                               & ~Q(reason_code__in=("configuration", "local_conditions")))
    )
    # Manual-cash-only: old auto-settled distributions are never treated as missing credits.
    credited = ProviderIncomeEntry.objects.filter(distribution_id=OuterRef("pk"), kind="credit")
    uncredited = splits.annotate(_credited=Exists(credited)).filter(
        status="succeeded", attention_reason="", evidence_conflict_code="",
        snapshot__income_mode="manual_cash_v1", snapshot__provider_amount__gt=0,
        _credited=False, updated_at__lte=now - timedelta(minutes=5),
    )
    withdrawals = ProviderWithdrawal.objects.filter(wallet__provider__in=providers)
    reserve = ProviderIncomeEntry.objects.filter(withdrawal_id=OuterRef("pk"), wallet_id=OuterRef("wallet_id"),
                                                kind="reserve", amount=OuterRef("amount"))
    terminal = ProviderIncomeEntry.objects.filter(withdrawal_id=OuterRef("pk"), wallet_id=OuterRef("wallet_id"),
        amount=OuterRef("amount")).filter(Q(kind="paid", withdrawal__status="succeeded") | Q(kind="release", withdrawal__status="failed"))
    withdrawals = withdrawals.annotate(_reserve_ok=Exists(reserve), _terminal_ok=Exists(terminal)).annotate(
        _ledger_gap=Case(When(Q(_reserve_ok=False) | (Q(status__in=("succeeded", "failed")) & Q(_terminal_ok=False)), then=Value(True)), default=Value(False))
    ).filter(
        Q(status__in=("unknown", "attention")) | Q(_ledger_gap=True)
        | Q(status="submitting", created_at__lte=now - timedelta(minutes=30))
        | Q(status="processing", created_at__lte=now - timedelta(hours=72))
    )
    wallets = wallet_balances(ProviderIncomeWallet.objects.filter(provider__in=providers)).filter(Q(_mismatch=True) | ~Q(hold_reason=""))
    wallet_version = MD5(Concat(*[item for name in (
        "available_amount", "reserved_amount", "paid_amount", "_expected_available", "_expected_reserved", "_expected_paid", "hold_reason",
    ) for item in (Cast(name, CharField()), Value(":"))]))
    return [
        Queue("distribution_attention", "分账结果待核查", "finance", flagged, "finance_alerts", "req_seq_id", version=split_version, hours=1),
        Queue("distribution_preflight_attention", "分账前置核验异常", "finance", preflight, "finance_alerts", "settlement__order__order_no",
              "checked_at", Concat("status", Value(":"), "reason_code"), hours=1, sort_field="checked_at"),
        Queue("distribution_credit_attention", "分账成功未入余额", "finance", uncredited, "finance_alerts", "req_seq_id", version="status", hours=1),
        Queue("withdrawal_attention", "提现结果及流水待核查", "finance", withdrawals, "finance_alerts", "req_seq_id",
              version=Concat("status", Value(":"), Cast("_ledger_gap", CharField())), hours=1),
        Queue("income_reconciliation", "达人余额核账", "finance", wallets, "finance_alerts", "provider__display_name",
              "updated_at", wallet_version, hours=1),
    ]


def finance_row(queue_key, obj):
    """Explicit safe fields only: no snapshot, card token, receiver ID, phone or raw channel payload."""
    checks = []
    if queue_key == "income_reconciliation":
        provider = obj.provider
        reference, amount, status = provider.display_name, obj.available_amount, "attention"
        reason = "余额汇总与收入流水不一致，请人工核账。" if obj._mismatch else "账户已暂停提现，需核实冻结原因。"
        for key, label in (("available", "可用余额"), ("reserved", "提现冻结"), ("paid", "累计提现")):
            checks.append({"label": label, "actual": getattr(obj, f"{key}_amount"), "expected": getattr(obj, f"_expected_{key}")})
        status_label, queried, order_no = "需人工核账", None, ""
    elif queue_key == "withdrawal_attention":
        provider = obj.wallet.provider
        reference, amount, status, status_label = obj.req_seq_id, obj.amount, obj.status, obj.get_status_display()
        reason = ("提现预留或终态流水缺失，请核账；不要重复申请提现。" if obj._ledger_gap else
                  "请核查原提现流水的最终结果；处理中不代表失败，禁止重复申请。")
        queried, order_no = obj.last_queried_at, ""
    else:
        provider, order_no = obj.settlement.provider, obj.settlement.order.order_no
        amount = obj.settlement.provider_settlement_amount
        status, status_label = obj.status, obj.get_status_display()
        if queue_key == "distribution_preflight_attention":
            reference, queried = order_no, obj.checked_at
            reason = obj.reason_message or "分账前置条件需人工核查，尚未发起渠道分账。"
        else:
            reference, queried = obj.req_seq_id, obj.last_queried_at
            recorded_amount = obj.snapshot.get("provider_amount")
            amount = recorded_amount if type(recorded_amount) is int and recorded_amount >= 0 else None
            reason = ("渠道分账已成功，但尚未找到对应收入入账流水；请核账，不要再次分账。" if queue_key == "distribution_credit_attention"
                      else "核查原分账流水及金额、费用和终态；禁止重发分账或直接改为成功。")
            if obj.evidence_conflict_code:
                reason = "分账证据存在冲突，账户仍需人工核账；后续查询成功不等于解除冻结。"
    return {"id": str(obj.pk), "reference": reference, "provider_name": provider.display_name,
            "city_code": provider.service_city_code, "amount": amount, "status": status, "status_label": status_label,
            "reason": reason, "checks": checks, "last_queried_at": queried, "order_no": order_no}
