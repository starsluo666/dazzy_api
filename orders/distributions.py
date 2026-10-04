"""Default-closed delayed split pilot. Verified new splits credit provider income."""

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from providers.cash_accounts import manual_cash_account
from .distribution_gateway import HuifuDistributionGateway, receivers
from .distribution_transport import (
    DistributionEvidenceConflict, DistributionUncertain, validate_transport,
)
from .distribution_preflight import (
    DistributionBlocked, mark_preflight_registered, record_preflight_failure,
)
from .huifu import HuifuConfigurationError, HuifuPaymentConfig, _canonical_digest
from .models import (
    ProviderOrder,
    ProviderOrderDistribution,
    ProviderOrderDistributionObservation,
    ProviderOrderPaymentOrder,
)
from .settlement_plans import sync_provider_settlement_plan

logger = logging.getLogger(__name__)


def _scope(config):
    return _canonical_digest(
        [
            "provider-distribution-v1",
            config.environment,
            config.sys_id,
            config.product_id,
            config.merchant_id,
            settings.HUIFU_USER_UPPER_ID.strip(),
        ]
    )


def _policy(config, *, provider_id, amount):
    if str(provider_id) not in settings.HUIFU_PROVIDER_DISTRIBUTION_IDS:
        raise ValidationError("达人不在分账测试白名单中。")
    if not settings.HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED:
        raise ValidationError("尚未确认渠道支付、分账及提现手续费由平台承担。")
    if (
        settings.HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS <= 0
        or not 0 < amount <= settings.HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS
    ):
        raise ValidationError("订单超出已配置的单笔分账测试额度。")
    if not settings.HUIFU_USER_UPPER_ID.strip():
        raise ValidationError("达人开户渠道范围未配置。")
    validate_transport(config)


def _receiver(provider_id, config, now):
    try:
        return manual_cash_account(provider_id, config, now=now)
    except ValidationError as exc:
        exc.preflight_code = "receiving_account"
        raise


def payment_cohort(order, allocation, config, *, now):
    """Called ONLY before a brand-new provider payment request is persisted."""
    if (
        not settings.HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED
        or allocation.wallet_amount
        or not allocation.external_amount
        or str(order.provider_id) not in settings.HUIFU_PROVIDER_DISTRIBUTION_IDS
    ):
        return {}
    _policy(config, provider_id=order.provider_id, amount=allocation.external_amount)
    account = _receiver(order.provider_id, config, now)
    return {
        "version": "provider-delayed-v1",
        "scope": _scope(config),
        "provider_id": order.provider_id,
        "receiver_id": account.user_huifu_id,
        "merchant_id": config.merchant_id,
        "fee_flag": config.fee_flag,
        "platform_fee_policy_confirmed": True,
    }


def _snapshot(order, config, *, now):
    # Caller holds the order lock. The same lock protects refunds and completion.
    plan = sync_provider_settlement_plan(order_no=order.order_no, now=now)
    if not plan or plan.status == "cancelled":
        raise ValidationError("订单没有可执行的分账准备单。")
    # Refund handling remains intact; only subsequent channel distribution is gated.
    # No assumptions about whether Huifu returns the original collection fee.
    if plan.refunded_amount or order.refund_orders.filter(status="succeeded").exists():
        raise DistributionBlocked("refunded_order")
    # The plan owns local readiness; channel checks below are independent proof.
    reasons = [item["message"] for item in plan.blockers]
    if reasons or plan.funding_type != "external" or plan.requires_manual_review:
        raise ValidationError(reasons or "仅支持新产生且来源已核对的全额外部支付订单。")
    payment = ProviderOrderPaymentOrder.objects.get(order=order)
    try:
        _policy(
            config, provider_id=order.provider_id, amount=plan.provider_amount + plan.platform_amount
        )
    except ValidationError as exc:
        exc.preflight_code = "configuration"
        raise
    account = _receiver(order.provider_id, config, now)
    cohort = payment.distribution_cohort
    # Fee mode belongs to the original payment, not today's environment setting.
    fee_flag = cohort.get("fee_flag") if isinstance(cohort, dict) else None
    expected_cohort = {
        "version": "provider-delayed-v1",
        "scope": _scope(config),
        "provider_id": order.provider_id,
        "receiver_id": account.user_huifu_id,
        "merchant_id": config.merchant_id,
        "fee_flag": fee_flag,
        "platform_fee_policy_confirmed": True,
    }
    if (
        payment.delay_acct_flag != "Y"
        or fee_flag not in {"1", "2"}
        or cohort != expected_cohort
        or payment.gateway_merchant_id != config.merchant_id
        or payment.channel not in {"wechat", "alipay"}
        or not payment.req_date
        or not payment.req_seq_id
        or not payment.gateway_trade_no
    ):
        raise ValidationError("支付单不是当前渠道范围内的新延时交易，禁止补分历史订单。")
    return {
        "version": "provider-distribution-v1",
        "scope": _scope(config),
        "order_no": order.order_no,
        "settlement_id": plan.settlement_id,
        "plan_revision": plan.revision,
        "merchant_id": config.merchant_id,
        "provider_id": order.provider_id,
        "payment": {
            "req_date": payment.req_date,
            "req_seq_id": payment.req_seq_id,
            "gateway_trade_no": payment.gateway_trade_no,
        },
        "paid_amount": plan.paid_amount,
        "refunded_amount": plan.refunded_amount,
        "provider_amount": plan.provider_amount,
        "income_mode": "manual_cash_v1",
        "receiver_id": account.user_huifu_id,
        "receiver_scope": account.channel_scope,
        "platform_amount": plan.platform_amount,
        "fee_flag": fee_flag,
    }


def execute_distribution(order_no):
    if not settings.HUIFU_PROVIDER_DISTRIBUTION_ENABLED:
        raise ValidationError("真实分账执行开关未开启。")
    # The durable request must commit before network I/O, never inside an outer tx.
    if transaction.get_connection().in_atomic_block:
        raise ValidationError("分账执行必须在独立事务外运行。")
    try:
        return _execute_distribution(order_no)
    except Exception as exc:
        default_code = (
            "configuration" if isinstance(exc, HuifuConfigurationError) else
            "local_conditions" if isinstance(exc, ValidationError) else "payment_proof"
        )
        code = getattr(exc, "preflight_code", getattr(exc, "code", default_code))
        record_preflight_failure(order_no, code)
        raise


def _execute_distribution(order_no):
    config = HuifuPaymentConfig.from_settings()
    with transaction.atomic():
        order = ProviderOrder.objects.select_for_update().get(order_no=order_no)
        existing = ProviderOrderDistribution.objects.filter(settlement__order=order).first()
        if existing:
            return existing, False  # Query is a separate, explicit operation.
        snapshot = _snapshot(order, config, now=timezone.now())
    gateway = HuifuDistributionGateway(config)
    evidence = gateway.verify_payment(snapshot)  # Read-only preflight outside locks.
    with transaction.atomic():
        order = ProviderOrder.objects.select_for_update().get(order_no=order_no)
        existing = ProviderOrderDistribution.objects.filter(settlement__order=order).first()
        if existing:
            return existing, False
        # Refunds, fee snapshots or the receiving account may have changed during I/O.
        current = _snapshot(order, config, now=timezone.now())
        if current != snapshot or not settings.HUIFU_PROVIDER_DISTRIBUTION_ENABLED:
            raise DistributionBlocked("snapshot_changed")
        record = ProviderOrderDistribution.objects.create(
            settlement_id=snapshot["settlement_id"],
            req_date=timezone.localdate().strftime("%Y%m%d"),
            req_seq_id="PD" + uuid.uuid4().hex[:30],
            snapshot={
                **snapshot,
                **evidence,
                "receivers": receivers(
                    snapshot["receiver_id"], snapshot["merchant_id"],
                    snapshot["provider_amount"], evidence["platform_split_amount"],
                ),
            },
            payment_fee_amount=evidence["payment_fee_amount"],
        )
        mark_preflight_registered(snapshot["settlement_id"])
    try:
        result = gateway.confirm(record)
    except Exception:
        # Even a process/config failure after the request was registered is uncertain.
        # No new sequence, even if a crash actually happened before the HTTP call.
        result = {"status": "unknown"}
    return _save_result(record.pk, result, queried=False), True


def query_distribution(order_no):
    record = ProviderOrderDistribution.objects.get(settlement__order__order_no=order_no)
    config = HuifuPaymentConfig.from_settings()
    if record.snapshot["scope"] != _scope(config):
        raise ValidationError("分账记录所属渠道范围与当前配置不同。")
    try:
        result = HuifuDistributionGateway(config).query(record)
    except DistributionEvidenceConflict as exc:
        result = {
            "status": "unknown", "evidence_conflict_code": exc.code,
            "response_code": exc.response_code, "response_digest": exc.response_digest,
        }
    except DistributionUncertain:
        result = {"status": "unknown"}
    return _save_result(record.pk, result, queried=True)


@transaction.atomic
def _save_result(record_id, result, *, queried):
    record = ProviderOrderDistribution.objects.select_for_update().get(pk=record_id)
    terminal = record.status in {"succeeded", "failed"}
    conflict = result.get("evidence_conflict_code", "") if queried else ""
    if terminal and queried and result["status"] in {"succeeded", "failed"}:
        if result["status"] != record.status:
            conflict = "query_terminal_conflict"
        elif record.status == "succeeded":
            if result.get("split_fee_amount") != record.split_fee_amount:
                conflict = "query_fee_conflict"
            elif result.get("gateway_trade_no", "") != record.gateway_trade_no:
                conflict = "query_trade_conflict"
    ProviderOrderDistributionObservation.objects.create(
        distribution=record,
        kind="query" if queried else "submit",
        status=result["status"],
        response_code=result.get("response_code", ""),
        response_digest=result.get("response_digest", ""),
        reason_code=conflict,
    )
    # Out-of-order query/submit responses never roll back a verified terminal state.
    if conflict:
        if not record.evidence_conflict_code:
            logger.error("Provider split evidence conflict: request=%s reason=%s", record.req_seq_id, conflict)
        record.evidence_conflict_code = record.evidence_conflict_code or conflict
        record.attention_reason = "渠道分账证据与原请求或已核验终态不一致，已暂停新增提现，请人工核账；禁止重发。"
        if not terminal:
            record.status = "unknown"
    elif record.evidence_conflict_code:
        pass  # Sticky hold: a later apparently normal response cannot clear a dispute.
    elif not terminal:
        for key in (
            "status",
            "response_code",
            "response_digest",
            "split_fee_amount",
            "gateway_trade_no",
        ):
            if key in result:
                setattr(record, key, result[key])
        record.attention_reason = (
            "渠道结果或费用明细待核实，仅可查询原流水。" if record.status == "unknown" else ""
        )
    if queried:
        record.last_queried_at = timezone.now()
    record.save()
    from providers.income_wallet import credit_distribution, hold_distribution_wallet
    if record.evidence_conflict_code or (record.attention_reason and terminal):
        hold_distribution_wallet(record)
    elif queried and record.status == "succeeded" and result["status"] == "succeeded":
        credit_distribution(record)
    return record


def assert_refund_not_distributed(order):
    # Even a verified F is manual review in this pilot; it must not become an
    # implicit retry/reversal authorization. Caller holds the order row lock.
    if ProviderOrderDistribution.objects.filter(settlement__order=order).exists():
        raise ValidationError(
            "订单已进入渠道分账流程，须核实并完成分账回退后再退款，请转异常交易处理。"
        )
