"""Default-closed, one-shot platform balance settlement for wallet/mixed orders.

Lock order: business order -> funding pool on registration; distribution -> pool
-> provider wallet on reconciliation. HTTP is always outside all DB transactions.
One unresolved payment per platform account deliberately favors safety over TPS.
"""

from dataclasses import asdict
import re
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from providers.cash_accounts import manual_cash_account
from wallets.models import WalletPaymentAllocation
from .balance_payment_gateway import BalancePaymentIntent, HuifuBalancePaymentGateway, payment_payload
from .distribution_preflight import DistributionBlocked, mark_preflight_registered, record_preflight_failure
from .distribution_transport import DistributionEvidenceConflict, DistributionUncertain, validate_transport
from .distributions import _scope, _save_result
from .huifu import HuifuConfigurationError, HuifuPaymentConfig, _canonical_digest
from .models import ProviderBalancePaymentPool, ProviderOrder, ProviderOrderDistribution
from .settlement_plans import sync_provider_settlement_plan


def rollout_start():
    try:
        value = parse_datetime(settings.HUIFU_PROVIDER_BALANCE_PAYMENT_START_AT)
    except (TypeError, ValueError):
        value = None
    return value if value and timezone.is_aware(value) else None


def _policy(config, amount):
    fee_reserve = settings.HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_RESERVE_CENTS
    limit = settings.HUIFU_PROVIDER_BALANCE_PAYMENT_MAX_CENTS
    acct_id = settings.HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID.strip()
    if (
        not settings.HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED
        or not settings.HUIFU_PROVIDER_BALANCE_PAYMENT_CONFIRMED
        or not settings.HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED
        or not settings.HUIFU_USER_UPPER_ID.strip()
        or type(limit) is not int or not 0 < amount <= limit
        or type(fee_reserve) is not int or not 0 <= fee_reserve < 10**13
        or not re.fullmatch(r"[A-Za-z0-9]{1,9}", acct_id)
        or rollout_start() is None
    ):
        raise DistributionBlocked("wallet_configuration")
    validate_transport(config)
    return {
        "acct_id": acct_id, "fee_reserve": fee_reserve,
        "fee_mode": settings.HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_MODE.strip(),
        "transfer_type": settings.HUIFU_PROVIDER_BALANCE_PAYMENT_TRANSFER_TYPE.strip(),
        "risk_ip": settings.HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP.strip(),
        "start_at": rollout_start().isoformat(),
    }


def _snapshot(order, config):
    from backoffice.manual_refunds import assert_no_manual_refund

    assert_no_manual_refund(order)
    plan = sync_provider_settlement_plan(order_no=order.order_no)
    if not plan or plan.status == "cancelled" or plan.funding_type not in ("wallet", "mixed"):
        raise DistributionBlocked("local_conditions")
    if plan.refunded_amount or order.refund_orders.filter(status="succeeded").exists():
        raise DistributionBlocked("refunded_order")
    preparation = plan.funding_snapshot.get("wallet_settlement_preparation") or {}
    if plan.requires_manual_review or preparation.get("business_blockers"):
        raise DistributionBlocked("local_conditions")
    if (
        not preparation or preparation.get("source_issues")
        or preparation.get("sources_truncated")
        or preparation.get("untraced_wallet_amount") != 0
        or preparation.get("traceable_wallet_amount") != preparation.get("wallet_remaining_amount")
    ):
        raise DistributionBlocked("wallet_source")
    policy = _policy(config, plan.provider_amount)
    if not order.paid_at or order.paid_at < rollout_start():
        raise DistributionBlocked("wallet_historical")
    try:
        account = manual_cash_account(order.provider_id, config, now=timezone.now())
    except ValidationError as exc:
        exc.preflight_code = "receiving_account"
        raise
    allocation = WalletPaymentAllocation.objects.get(
        business_type="provider_order", business_order_no=order.order_no,
    )
    sources = []
    for debit in allocation.lot_debits.select_related("lot__recharge_order").order_by("pk"):
        recharge = debit.lot.recharge_order
        sources.append({
            "kind": "recharge", "reference": recharge.order_no,
            "req_date": recharge.req_date, "req_seq_id": recharge.req_seq_id,
            "gateway_trade_no": recharge.gateway_trade_no, "amount": recharge.payable_amount,
        })
    if allocation.external_amount:
        payment = order.payment_order
        sources.append({
            "kind": "external", "reference": payment.payment_no,
            "req_date": payment.req_date, "req_seq_id": payment.req_seq_id,
            "gateway_trade_no": payment.gateway_trade_no, "amount": allocation.external_amount,
        })
    # Validate real business/risk fields before any channel call. Sequence/date
    # below are validation-only; the real immutable values are generated on commit.
    payment_payload(BalancePaymentIntent(
        req_date=timezone.localdate().strftime("%Y%m%d"), req_seq_id="validation",
        merchant_id=config.merchant_id, receiver_id=account.user_huifu_id,
        amount=plan.provider_amount, fee_mode=policy["fee_mode"],
        transfer_type=policy["transfer_type"], risk_ip=policy["risk_ip"],
        out_acct_id=policy["acct_id"],
    ))
    return {
        "version": "provider-balance-payment-v1", "scope": _scope(config),
        # One physical account, even if product/system configuration changes.
        # Changing credentials must never bypass an outstanding funds reservation.
        "pool_scope": _canonical_digest(["provider-balance-pool-v1", config.environment, config.merchant_id, policy["acct_id"]]),
        "order_no": order.order_no, "settlement_id": plan.settlement_id,
        "plan_revision": plan.revision, "merchant_id": config.merchant_id,
        "provider_id": order.provider_id, "receiver_id": account.user_huifu_id,
        "receiver_scope": account.channel_scope, "income_mode": "manual_cash_v1",
        "provider_amount": plan.provider_amount, "platform_amount": plan.platform_amount,
        "funding_type": plan.funding_type, "funding_digest": _canonical_digest(plan.funding_snapshot),
        "policy": policy, "sources": sources,
    }


def _locked_pool(scope):
    pool, _ = ProviderBalancePaymentPool.objects.get_or_create(scope=scope)
    pool = ProviderBalancePaymentPool.objects.select_for_update().get(pk=pool.pk)
    if pool.hold_reason:
        raise DistributionBlocked("wallet_pool_hold")
    if pool.payments.filter(funds_released=False).exists():
        raise DistributionBlocked("wallet_busy")
    return pool


def execute_wallet_distribution(order_no):
    if not settings.HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED:
        raise DistributionBlocked("wallet_configuration")
    if transaction.get_connection().in_atomic_block:
        raise ValidationError("余额结算必须在独立事务外运行。")
    try:
        return _execute(order_no)
    except Exception as exc:
        code = getattr(exc, "preflight_code", getattr(exc, "code", "wallet_source"))
        if isinstance(exc, HuifuConfigurationError) or code in ("account_payment_intent", "account_payment_merchant"):
            code = "wallet_configuration"
        record_preflight_failure(order_no, code)
        raise


def _execute(order_no):
    config = HuifuPaymentConfig.from_settings()
    with transaction.atomic():
        order = ProviderOrder.objects.select_for_update().get(order_no=order_no)
        existing = ProviderOrderDistribution.objects.filter(settlement__order=order).first()
        if existing:
            return existing, False
        snapshot = _snapshot(order, config)
        generation = _locked_pool(snapshot["pool_scope"]).generation

    gateway = HuifuBalancePaymentGateway(config, allow_submit=True)
    source_proofs = [
        gateway.verify_source(source, acct_id=snapshot["policy"]["acct_id"])
        for source in snapshot["sources"]
    ]
    started = time.monotonic()
    balance = gateway.platform_balance(snapshot["policy"]["acct_id"])
    with transaction.atomic():
        order = ProviderOrder.objects.select_for_update().get(order_no=order_no)
        existing = ProviderOrderDistribution.objects.filter(settlement__order=order).first()
        if existing:
            return existing, False
        if _snapshot(order, config) != snapshot:
            raise DistributionBlocked("snapshot_changed")
        pool = _locked_pool(snapshot["pool_scope"])
        if pool.generation != generation or time.monotonic() - started > 30:
            raise DistributionBlocked("snapshot_changed")
        reserved = snapshot["provider_amount"] + snapshot["policy"]["fee_reserve"]
        if balance["available_amount"] < reserved:
            raise DistributionBlocked("wallet_funds")
        date, seq = timezone.localdate().strftime("%Y%m%d"), "PB" + uuid.uuid4().hex[:30]
        intent = BalancePaymentIntent(
            req_date=date, req_seq_id=seq, merchant_id=config.merchant_id,
            receiver_id=snapshot["receiver_id"], amount=snapshot["provider_amount"],
            transfer_type=snapshot["policy"]["transfer_type"], risk_ip=snapshot["policy"]["risk_ip"],
            fee_mode=snapshot["policy"]["fee_mode"], out_acct_id=snapshot["policy"]["acct_id"],
        )
        record = ProviderOrderDistribution.objects.create(
            settlement_id=snapshot["settlement_id"], route="balance", req_date=date, req_seq_id=seq,
            payment_fee_amount=None, funding_pool=pool, funds_reserved_amount=reserved,
            snapshot={**snapshot, "intent": asdict(intent), "source_query_digests": source_proofs,
                      "funds_query_digest": balance["response_digest"],
                      "funds_available_amount": balance["available_amount"],
                      "funds_checked_at": timezone.now().isoformat()},
        )
        pool.generation += 1
        pool.save()
        mark_preflight_registered(snapshot["settlement_id"])
    # No retry after registration, even if the process crashes before this call.
    try:
        result = gateway.pay(intent)
    except DistributionEvidenceConflict as exc:
        result = conflict_result(exc)
    except Exception:
        result = {"status": "unknown"}
    return _save_result(record.pk, result, queried=False), True


def conflict_result(exc):
    return {"status": "unknown", "evidence_conflict_code": exc.code,
            "response_code": exc.response_code, "response_digest": exc.response_digest}


def query_wallet_distribution(record, config):
    if transaction.get_connection().in_atomic_block:
        raise ValidationError("余额付款查询必须在独立事务外运行。")
    if record.snapshot["scope"] != _scope(config):
        raise ValidationError("余额付款所属渠道范围与当前配置不同。")
    intent = BalancePaymentIntent(**record.snapshot["intent"])
    if (intent.req_date != record.req_date or intent.req_seq_id != record.req_seq_id
            or intent.amount != record.snapshot["provider_amount"]
            or intent.receiver_id != record.snapshot["receiver_id"]):
        raise ValidationError("余额付款持久化流水与快照不一致，禁止查询其他交易入账。")
    try:
        result = HuifuBalancePaymentGateway(config).query(intent, known_trade_no=record.gateway_trade_no)
        result["account_payment_fee_amount"] = result.pop("channel_fee_amount", None)
    except DistributionEvidenceConflict as exc:
        result = conflict_result(exc)
    except DistributionUncertain:
        result = {"status": "unknown"}
    return _save_result(record.pk, result, queried=True)


def reconcile_pool(record):
    """Called inside result/credit transaction; uncertainty never releases a slot."""
    pool = ProviderBalancePaymentPool.objects.select_for_update().get(pk=record.funding_pool_id)
    if record.evidence_conflict_code or (record.status == "failed" and not record.failure_reviewed_at):
        reason = "余额付款失败或证据存在差异，请财务核账后再处理；禁止自动重发。"
        if not pool.hold_reason:
            pool.hold_reason = reason
            pool.generation += 1
            pool.save()
    elif record.status == "succeeded" and not record.funds_released:
        record.funds_released = True
        record.save(update_fields=("funds_released",))
        pool.generation += 1
        pool.save()
