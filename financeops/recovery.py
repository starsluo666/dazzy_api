"""Audited exceptions, not a generic ledger editor or retry-payment facility.

Only a verified, non-conflicting FAILED account payment may release its local
single-flight slot. Its original failed transaction stays immutable/non-retryable.
Provider wallet holds, bank returns and conflicting evidence stay manual.
"""
from datetime import timedelta
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from orders.huifu import HuifuPaymentConfig, _canonical_digest
from orders.models import ProviderBalancePaymentPool as Pool, ProviderOrderDistribution as Distribution
from providers.models import ProviderIncomeEntry, ProviderIncomeWallet as Wallet, ProviderWithdrawal as Withdrawal
from .models import FinanceRecoveryCase as Case, FinanceRecoveryEvent as Event

TARGETS = {"distribution": Distribution, "withdrawal": Withdrawal, "wallet": Wallet, "pool": Pool}


def target(case):
    return get_object_or_404(TARGETS[case.kind], pk=case.target_id)


def fingerprint(case):
    obj = target(case)
    if case.kind == "pool":
        data = [obj.scope, obj.generation, obj.hold_reason,
                list(obj.payments.filter(Q(funds_released=False) | ~Q(evidence_conflict_code="")).order_by("pk").values(
                    "id", "req_date", "req_seq_id", "status", "snapshot", "gateway_trade_no",
                    "evidence_conflict_code", "attention_reason", "funds_reserved_amount",
                    "funds_released", "failure_reviewed_at",
                ))]
    elif case.kind == "wallet":
        data = [obj.channel_scope, obj.receiver_id, obj.available_amount, obj.reserved_amount,
                obj.paid_amount, obj.hold_reason, obj.entries.aggregate(
                    count=Count("pk"), latest=Max("pk"), available=Sum("available_delta"),
                    reserved=Sum("reserved_delta"), paid=Sum("amount", filter=Q(kind="paid")))]
    else:
        data = [obj.req_date, obj.req_seq_id, obj.snapshot, obj.status, obj.gateway_trade_no]
        if case.kind == "distribution":
            data += [obj.evidence_conflict_code, obj.attention_reason, obj.split_fee_amount,
                     obj.account_payment_fee_amount]
        else:
            data += [obj.fee_amount, fingerprint(Case(kind="wallet", target_id=obj.wallet_id))]
    # Datetimes converted for canonical JSON without persisting any raw response.
    import json
    return _canonical_digest(json.loads(json.dumps(data, default=str)))


def reference(kind, obj):
    if kind in ("distribution", "withdrawal"):
        return obj.req_seq_id
    return f"平台付款账户 #{obj.pk}" if kind == "pool" else f"达人收入账户 #{obj.pk}"


@transaction.atomic
def create_case(*, kind, target_id, actor, note):
    obj = get_object_or_404(TARGETS[kind].objects.select_for_update(), pk=target_id)
    existing = Case.objects.filter(kind=kind, target_id=target_id, status__in=("open", "review")).first()
    if existing:
        return existing
    case = Case.objects.create(kind=kind, target_id=target_id, reference=reference(kind, obj), created_by=actor)
    Event.objects.create(case=case, actor=actor, action="create", note=note, revision=1)
    return case


def pool_candidate(case):
    if case.kind != "pool":
        raise ValidationError("此类异常仅支持核查留痕；不能在线解冻、调账或补付。")
    pool = target(case)
    records = list(pool.payments.filter(funds_released=False))
    if not pool.hold_reason or len(records) != 1:
        raise ValidationError("账户暂停或待核验原流水已变化，请重新核查。")
    record = records[0]
    if (record.route != "balance" or record.status != "failed" or record.evidence_conflict_code or record.attention_reason
            or record.failure_reviewed_at or pool.payments.exclude(evidence_conflict_code="").exists()):
        raise ValidationError("只有无证据冲突、已明确失败的余额付款可以解除后续暂停；未知结果和终态差异须继续人工核账。")
    amount = record.snapshot.get("provider_amount")
    reserve = record.snapshot.get("policy", {}).get("fee_reserve")
    if (type(amount) is not int or amount <= 0 or type(reserve) is not int or reserve < 0
            or record.funds_reserved_amount != amount + reserve
            or ProviderIncomeEntry.objects.filter(distribution=record).exists()):
        raise ValidationError("原预留或入账流水不一致，请继续人工核账，不能解除暂停。")
    return record


def query_original(case):
    """Explicit POST only; reuses signed adapters and their idempotent bookkeeping."""
    if case.kind == "distribution":
        from orders.distributions import query_distribution
        query_distribution(target(case).settlement.order.order_no)
    elif case.kind == "withdrawal":
        from providers.withdrawals import query_withdrawal
        query_withdrawal(target(case))
    elif case.kind == "pool":
        from orders.distributions import query_distribution
        rows = list(target(case).payments.filter(funds_released=False))
        if len(rows) != 1:
            raise ValidationError("当前无唯一待核验流水，请按出款记录逐笔核查。")
        query_distribution(rows[0].settlement.order.order_no)
    else:
        raise ValidationError("账本没有单一渠道流水，请从相关出款或提现记录查询；不提供自动补账。")


def prove_pool_failure(case):
    """Fresh signed original failure + current account balance; no money submit."""
    from orders.distributions import _scope
    from orders.balance_payment_gateway import HuifuBalancePaymentGateway
    from orders.wallet_distributions import query_wallet_distribution

    record = pool_candidate(case)
    config = HuifuPaymentConfig.from_settings()
    if record.snapshot["scope"] != _scope(config):
        raise ValidationError("当前渠道配置与原流水不一致。")
    pool_scope = _canonical_digest([
        "provider-balance-pool-v1", config.environment, config.merchant_id, record.snapshot["policy"]["acct_id"],
    ])
    if target(case).scope != pool_scope:
        raise ValidationError("出款账户范围不一致，禁止解除暂停。")
    query_wallet_distribution(record, config)
    record = pool_candidate(case)
    observation = record.observations.order_by("-pk").first()
    if (not observation or observation.kind != "query" or observation.status != "failed"
            or observation.reason_code or observation.response_code != "00000000"
            or not observation.response_digest or observation.created_at < timezone.now() - timedelta(seconds=30)):
        raise ValidationError("尚无本次原流水失败的有效查询证据，不能解除暂停。")
    balance = HuifuBalancePaymentGateway(config).platform_balance(record.snapshot["policy"]["acct_id"])
    # Conservative: failure alone does not authorize releasing a reservation.
    if (not balance.get("response_digest") or type(balance.get("available_amount")) is not int
            or balance["available_amount"] < (record.funds_reserved_amount or 0)):
        raise ValidationError("渠道可用资金尚不足以核实原预留，继续保留暂停。")
    return {
        "record_id": record.pk, "observation_id": observation.pk,
        "original_query_digest": observation.response_digest,
        "balance_query_digest": balance["response_digest"],
        "available_amount": balance["available_amount"],
        "required_amount": record.funds_reserved_amount,
    }


def _validate_revision(case, revision):
    if case.revision != revision:
        raise ValidationError("记录已更新，请刷新详情后重试。")
    if case.lease_until and case.lease_until > timezone.now():
        raise ValidationError("此记录正在核验，请稍后刷新。")


def action(case_id, *, actor, revision, action, note, evidence_reference="", resolution="record"):
    if action not in ("query", "submit", "approve", "return") or resolution not in ("record", "resume"):
        raise ValidationError("不支持的处置操作。")
    # Never keep row locks during network I/O. Lease+revision fence later writes.
    with transaction.atomic():
        case = Case.objects.select_for_update().get(pk=case_id)
        _validate_revision(case, revision)
        if action == "approve" and (
            case.created_by_id == actor.pk or case.submitted_by_id == actor.pk
        ):
            raise ValidationError("请由未参与申请和提交的另一位财务独立复核。")
        if action in ("approve", "return") and case.status != "review":
            raise ValidationError("当前记录不在待复核状态。")
        if action in ("query", "submit") and case.status != "open":
            raise ValidationError("当前记录不可查询或提交，请先退回核查。")
        if action == "query" and case.queried_at and case.queried_at > timezone.now() - timedelta(seconds=60):
            raise ValidationError("原流水刚刚查询过，请至少等待一分钟再查询。")
        if action == "submit":
            if not evidence_reference:
                raise ValidationError("请填写内部核账凭证编号。")
            if resolution == "resume":
                if not settings.HUIFU_FINANCE_RECOVERY_ENABLED:
                    raise ValidationError("受控恢复尚未启用；仍可登记核查结果。")
                pool_candidate(case)
            case.resolution, case.submitted_fingerprint = resolution, fingerprint(case)
            case.submitted_by, case.status = actor, "review"
        elif action == "return":
            case.status = "open"
        if action in ("submit", "return"):
            case.revision += 1
            case.save()
            Event.objects.create(case=case, actor=actor, action=action, note=note,
                                 evidence_reference=evidence_reference, revision=case.revision)
            return case
        if action == "approve" and fingerprint(case) != case.submitted_fingerprint:
            raise ValidationError("资金记录已变化，请退回重新核查，不能沿用旧凭证。")
        if action == "approve" and case.resolution == "resume" and not settings.HUIFU_FINANCE_RECOVERY_ENABLED:
            raise ValidationError("受控恢复尚未启用。")
        token = uuid.uuid4()
        if action == "query":
            case.queried_at = timezone.now()
        case.run_token, case.lease_until = token, timezone.now() + timedelta(minutes=5)
        case.save()

    started = time.monotonic()
    proof = None
    try:
        if action == "query":
            query_original(case)
        elif case.resolution == "resume":
            proof = prove_pool_failure(case)
        with transaction.atomic():
            case = Case.objects.select_for_update().get(pk=case_id)
            if case.run_token != token or case.revision != revision or case.lease_until <= timezone.now():
                raise ValidationError("核验已过期，请重新核查。")
            if proof:
                # Same order as the existing query handler: distribution -> pool.
                record = Distribution.objects.select_for_update().get(pk=proof["record_id"])
                pool = Pool.objects.select_for_update().get(pk=record.funding_pool_id)
                if time.monotonic() - started > 30 or fingerprint(case) != case.submitted_fingerprint:
                    raise ValidationError("核验超时或资金证据已变化，请退回后重新提交。")
                pool_candidate(case)
                if record.observations.order_by("-pk").values_list("pk", flat=True).first() != proof["observation_id"]:
                    raise ValidationError("原流水出现新的查询结果，请退回后重新核查。")
                record.funds_released = True
                record.failure_reviewed_at = timezone.now()
                record.save(update_fields=("funds_released", "failure_reviewed_at"))
                pool.hold_reason, pool.generation = "", pool.generation + 1
                pool.save()
            elif action == "approve" and fingerprint(case) != case.submitted_fingerprint:
                raise ValidationError("资金记录已变化，请退回重新核查。")
            if action == "approve":
                case.status = "restored" if proof else "resolved"
            else:
                case.queried_at = timezone.now()
            case.revision += 1
            case.lease_until = None
            case.save()
            Event.objects.create(case=case, actor=actor, action=action, note=note,
                                 evidence_reference=evidence_reference, revision=case.revision,
                                 verification=proof or {})
            return case
    except Exception as exc:
        with transaction.atomic():
            current = Case.objects.select_for_update().get(pk=case_id)
            if current.run_token == token:
                current.lease_until = None
                current.revision += 1
                current.save()
                Event.objects.create(case=current, actor=actor, action="check_failed",
                                     note="核验未完成，未执行受控恢复；请重新查看原流水。", revision=current.revision)
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("渠道核验暂未完成，请稍后查询原流水；未发起补付或解除暂停。") from None
