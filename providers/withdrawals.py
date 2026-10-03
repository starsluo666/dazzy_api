"""Reserve before cash I/O, persist one request, query-only on every uncertain outcome."""

from types import SimpleNamespace
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import ValidationError

from accounts.account_closure import lock_active_user_for_business
from orders.distribution_transport import DistributionUncertain, validate_transport
from orders.distributions import _scope
from orders.huifu import HuifuPaymentConfig
from .cash_accounts import manual_cash_account
from .huifu_user import digest
from .income_wallet import locked_wallet, append_entry
from .models import (
    ProviderIncomeWallet,
    ProviderWithdrawal,
    ProviderWithdrawalObservation,
    ProviderProfile,
)
from .receiving_accounts import decrypt_details
from .receiving_onboarding import refresh_onboarding
from .withdrawal_gateway import HuifuWithdrawalGateway


def _enabled(provider, amount, config):
    if not settings.HUIFU_PROVIDER_WITHDRAWAL_ENABLED:
        raise ValidationError("余额提现暂未开放，收入余额会保留，请等待平台开通。")
    if str(provider.pk) not in settings.HUIFU_PROVIDER_DISTRIBUTION_IDS:
        raise ValidationError("当前账户暂未开放提现。")
    if not settings.HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED:
        raise ValidationError("平台承担提现手续费的渠道规则尚未确认。")
    if type(amount) is not int or not 0 < amount <= settings.HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS:
        raise ValidationError("提现金额须大于零且不超过单笔限额。")
    validate_transport(config)


@sensitive_variables()
def _cash_token(account):
    return decrypt_details(
        SimpleNamespace(provider_id=account.provider_id, details_ciphertext=account.cash_card_ciphertext)
    )["token_no"]


@sensitive_variables()
def _fingerprint(account):
    # Compare the verified token's identity, not randomized AES-GCM ciphertext.
    # This digest is ephemeral: never persisted, returned or logged.
    return digest(
        [
            account.pk,
            account.user_huifu_id,
            account.channel_scope,
            _cash_token(account),
            account.verified_cash_config,
        ]
    )


def _replay(existing, amount):
    if existing.amount != amount:
        raise ValidationError("同一提现请求不能修改金额，请先查询原申请。")
    return existing, False


@sensitive_variables()
def create_withdrawal(provider, *, amount, request_key):
    if transaction.get_connection().in_atomic_block:
        raise ValidationError("提现须在独立事务外执行。")
    try:
        request_key = uuid.UUID(str(request_key))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("提现请求标识无效。") from None
    existing = ProviderWithdrawal.objects.filter(
        wallet__provider=provider, request_key=request_key
    ).first()
    if existing:
        return _replay(existing, amount)
    config = HuifuPaymentConfig.from_settings()
    _enabled(provider, amount, config)
    # Read-only local preflight before channel queries; no debt/negative balance.
    wallet = ProviderIncomeWallet.objects.filter(provider=provider).first()
    if not wallet or wallet.available_amount < amount:
        raise ValidationError("可提现余额不足，待结算收入暂不能提现。")
    if wallet.hold_reason or wallet.reserved_amount:
        raise ValidationError(wallet.hold_reason or "已有提现处理中，请先等待或刷新结果。")
    check_started = timezone.now()
    refresh_onboarding(provider)  # Must get a NEW verified manual-cash + own-card query.
    account = manual_cash_account(provider.pk, config)
    if account.channel_checked_at < check_started:
        raise ValidationError("收款账户正在核验或查询未完成，请稍后重试。")
    fingerprint = _fingerprint(account)
    token = _cash_token(account)
    gateway = HuifuWithdrawalGateway(config)
    try:
        balance = gateway.balance(account.user_huifu_id)
    except DistributionUncertain:
        raise ValidationError("渠道可用余额暂未核实，尚未发起提现，请稍后重试。") from None
    with transaction.atomic():
        lock_active_user_for_business(provider.user)
        wallet = locked_wallet(
            provider.pk, scope=account.channel_scope, receiver_id=account.user_huifu_id
        )
        existing = wallet.withdrawals.filter(request_key=request_key).first()
        if existing:
            return _replay(existing, amount)
        _enabled(provider, amount, config)
        current = manual_cash_account(provider.pk, config, lock=True)
        if _fingerprint(current) != fingerprint:
            raise ValidationError("收款配置已变化，请重新核验后提现。")
        if wallet.hold_reason or wallet.reserved_amount:
            raise ValidationError(wallet.hold_reason or "已有提现处理中，请先刷新结果。")
        if balance["available_amount"] < wallet.available_amount:
            raise ValidationError("渠道余额与收入账务尚未核对一致，请联系平台核账。")
        record = ProviderWithdrawal.objects.create(
            wallet=wallet,
            request_key=request_key,
            amount=amount,
            req_seq_id="PW" + uuid.uuid4().hex[:30],
            req_date=timezone.localdate().strftime("%Y%m%d"),
            snapshot={
                "scope": _scope(config),
                "receiver_id": account.user_huifu_id,
                "acct_id": balance["acct_id"],
                "receiver_scope": account.channel_scope,
                "bank_card_masked": account.bank_card_masked,
                "bank_name": account.bank_name,
                "cash_type": account.verified_cash_config["cash_type"],
                "cash_config": account.verified_cash_config,
                "fee_bearer": "platform",
                "balance_evidence": balance["response_digest"],
            },
        )
        append_entry(
            wallet,
            kind="reserve",
            amount=amount,
            source_key=f"withdrawal:{record.pk}:reserve",
            withdrawal=record,
        )
    # No retry even on a process/network error. The durable row is recoverable by query.
    try:
        result = gateway.submit(record, token)
    except Exception:
        result = {"status": "unknown"}
    return save_result(record.pk, result, queried=False), True


def query_withdrawal(record):
    config = HuifuPaymentConfig.from_settings()
    if record.snapshot["scope"] != _scope(config):
        raise ValidationError("提现所属渠道与当前配置不一致，请联系平台核账。")
    gateway = HuifuWithdrawalGateway(config)
    balance = None
    try:
        result = gateway.query(record)
        if result["status"] == "failed":
            balance = gateway.balance(record.snapshot["receiver_id"])
            if balance["acct_id"] != record.snapshot["acct_id"]:
                raise DistributionUncertain()
    except DistributionUncertain:
        result = {"status": "unknown"}
    return save_result(record.pk, result, queried=True, balance=balance)


@transaction.atomic
def save_result(record_id, result, *, queried, balance=None):
    reference = ProviderWithdrawal.objects.select_related("wallet").get(pk=record_id)
    ProviderProfile.objects.select_for_update().get(pk=reference.wallet.provider_id)
    wallet = ProviderIncomeWallet.objects.select_for_update().get(pk=reference.wallet_id)
    record = ProviderWithdrawal.objects.select_for_update().get(pk=record_id)
    ProviderWithdrawalObservation.objects.create(
        withdrawal=record,
        kind="query" if queried else "submit",
        status=result["status"],
        response_code=result.get("response_code", ""),
        response_digest=result.get("response_digest", ""),
    )
    terminal = record.status in {"succeeded", "failed", "attention"}
    if terminal:
        if (
            queried
            and result["status"] in {"succeeded", "failed", "attention"}
            and record.status != result["status"]
        ):
            record.status = "attention"
            wallet.hold_reason = "提现终态发生差异或银行卡退汇，请联系平台核账。"
            wallet.save()
    else:
        state = result["status"]
        if not queried and state in {"succeeded", "failed"}:
            state = "processing"
        if state == "failed" and (
            not balance or balance["available_amount"] < wallet.available_amount + record.amount
        ):
            state = "unknown"  # Failure alone does not prove funds have returned.
        record.status = state
        for key in ("response_code", "response_digest", "fee_amount", "gateway_trade_no"):
            if key in result:
                setattr(record, key, result[key])
        if state in {"succeeded", "failed"}:
            append_entry(
                wallet,
                kind="paid" if state == "succeeded" else "release",
                amount=record.amount,
                source_key=f"withdrawal:{record.pk}:terminal",
                withdrawal=record,
            )
        elif state == "attention":
            wallet.hold_reason = "提现发生银行卡退汇或渠道状态异常，请联系平台核账。"
            wallet.save()
    if queried:
        record.last_queried_at = timezone.now()
    record.save()
    return record


def withdrawal_data(record):
    return {
        "withdrawal_no": record.req_seq_id,
        "amount": record.amount,
        "status": record.status,
        "status_label": record.get_status_display(),
        "bank_card_masked": record.snapshot.get("bank_card_masked", ""),
        "bank_name": record.snapshot.get("bank_name", ""),
        "cash_type": record.snapshot.get("cash_type", ""),
        "provider_fee_amount": 0,
        "platform_fee_amount": record.fee_amount,
        "created_at": record.created_at,
        "last_queried_at": record.last_queried_at,
    }


def income_wallet_data(provider):
    wallet = ProviderIncomeWallet.objects.filter(provider=provider).first()
    available = wallet.available_amount if wallet else 0
    reason = ""
    config = HuifuPaymentConfig.from_settings()
    try:
        _enabled(
            provider, max(1, min(available, settings.HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS)), config
        )
        # Let an otherwise eligible user START preflight after cache expiry.
        # create_withdrawal still requires a new signed account query and balance.
        account = manual_cash_account(provider.pk, config, require_fresh=False)
        if wallet and (
            wallet.receiver_id != account.user_huifu_id
            or wallet.channel_scope != account.channel_scope
        ):
            raise ValidationError("收入所属渠道账户已变化，请联系平台核账。")
    except Exception:
        # No channel calls or private configuration errors on income-page reads.
        reason = "提现尚未就绪，请检查收款账户或联系平台。"
    if not settings.HUIFU_PROVIDER_WITHDRAWAL_ENABLED:
        reason = "提现暂未开放，余额会保留。"
    if wallet and (wallet.hold_reason or wallet.reserved_amount):
        reason = wallet.hold_reason or "已有提现处理中，请等待或刷新结果。"
    if not reason and not available:
        reason = "分账核验成功后，收入才会计入可提现余额。"
    return {
        "available_amount": available,
        "reserved_amount": wallet.reserved_amount if wallet else 0,
        "paid_amount": wallet.paid_amount if wallet else 0,
        "can_withdraw": not bool(reason),
        "unavailable_reason": reason,
        "max_withdrawal_amount": settings.HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS,
        "withdrawals": [withdrawal_data(row) for row in wallet.withdrawals.all()[:50]]
        if wallet
        else [],
    }
