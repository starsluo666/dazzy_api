"""Send a reviewed reward once and credit it only after signed channel inquiry."""

from dataclasses import asdict
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.account_closure import lock_active_user_for_business
from orders.balance_payment_gateway import BalancePaymentIntent, HuifuBalancePaymentGateway, payment_payload
from orders.distributions import _scope
from orders.distribution_transport import DistributionEvidenceConflict
from orders.huifu import HuifuPaymentConfig, _canonical_digest
from orders.models import ProviderBalancePaymentPool, ProviderOrder, ProviderOrderSettlement
from orders.wallet_distributions import _locked_pool
from providers.cash_accounts import manual_cash_account
from providers.income_wallet import append_entry, locked_wallet
from providers.models import ProviderIncomeWallet, ProviderProfile
from providers.receiving_onboarding import refresh_onboarding

from .first_order_rewards import eligible_first_order
from .models import ProviderInvitePayout, ProviderInviteReward


def _policy(amount):
    if not (
        settings.HUIFU_INVITE_REWARD_PAYOUT_ENABLED
        and settings.HUIFU_INVITE_REWARD_PAYOUT_CONFIRMED
        and settings.HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED
        and 0 < amount <= settings.HUIFU_INVITE_REWARD_PAYOUT_MAX_CENTS
        and settings.HUIFU_INVITE_REWARD_PAYOUT_ACCT_ID.strip()
        and settings.HUIFU_INVITE_REWARD_PAYOUT_FEE_RESERVE_CENTS >= 0
    ):
        raise ValidationError("邀请奖励汇付出款配置尚未就绪，或金额超过限额。")
    return {
        "out_acct_id": settings.HUIFU_INVITE_REWARD_PAYOUT_ACCT_ID.strip(),
        "transfer_type": settings.HUIFU_INVITE_REWARD_PAYOUT_TRANSFER_TYPE,
        "risk_ip": settings.HUIFU_INVITE_REWARD_PAYOUT_RISK_IP,
        "fee_mode": settings.HUIFU_INVITE_REWARD_PAYOUT_FEE_MODE,
    }


def _receiver(reward):
    user_id = reward.source.provider_user_id if reward.source.kind == "provider" else reward.source.store_user_id
    status = ProviderProfile.Status.APPROVED if reward.source.kind == "provider" else ProviderProfile.Status.STORE
    return ProviderProfile.objects.select_related("user").get(
        user_id=user_id, status=status, user__is_active=True, user__account_status="active",
    )


def _reward(reward_id):
    return ProviderInviteReward.objects.select_related("source").get(pk=reward_id)


def submit_reward(reward_id):
    if transaction.get_connection().in_atomic_block:
        raise ValidationError("奖励划款必须在独立事务外执行。")
    reward = _reward(reward_id)
    if reward.status != "approved" or reward.payout_method != "balance" or hasattr(reward, "payout"):
        return False
    policy = _policy(reward.amount)
    config = HuifuPaymentConfig.from_settings()
    gateway = HuifuBalancePaymentGateway(config, allow_submit=True)
    receiver = _receiver(reward)
    account = manual_cash_account(receiver.pk, config, require_fresh=False)
    intent = BalancePaymentIntent(
        req_date=timezone.localdate().strftime("%Y%m%d"), req_seq_id="IR" + uuid.uuid4().hex[:30],
        merchant_id=config.merchant_id, receiver_id=account.user_huifu_id, amount=reward.amount,
        **policy,
    )
    payment_payload(intent)
    pool_scope = _canonical_digest([
        "provider-balance-pool-v1", config.environment, config.merchant_id, intent.out_acct_id,
    ])
    with transaction.atomic():
        _locked_pool(pool_scope)
    checking_since = timezone.now()
    refresh_onboarding(receiver)
    with transaction.atomic():
        generation = _locked_pool(pool_scope).generation
    started = time.monotonic()
    balance = gateway.platform_balance(intent.out_acct_id)
    with transaction.atomic():
        if reward.order_id:
            ProviderOrder.objects.select_for_update().get(pk=reward.order_id)
            settlement = ProviderOrderSettlement.objects.select_for_update().get(order_id=reward.order_id)
            if not eligible_first_order(settlement):
                raise ValidationError("首单状态已变化，请财务核实。")
        current = ProviderInviteReward.objects.select_for_update(of=("self",)).select_related("source").get(pk=reward_id)
        if (current.status != "approved" or current.payout_method != "balance"
                or current.amount != intent.amount or current.order_id != reward.order_id
                or _policy(current.amount) != policy or hasattr(current, "payout")):
            return False
        locked_receiver = _receiver(current)
        if locked_receiver.pk != receiver.pk:
            raise ValidationError("邀请奖励收款人已变化，请财务核实。")
        lock_active_user_for_business(locked_receiver.user)
        ProviderProfile.objects.select_for_update().get(pk=receiver.pk)
        current_account = manual_cash_account(receiver.pk, config, lock=True)
        if (current_account.channel_checked_at < checking_since
                or current_account.user_huifu_id != intent.receiver_id or current_account.channel_scope != account.channel_scope
                or ProviderIncomeWallet.objects.filter(provider=receiver).exclude(
                    receiver_id=intent.receiver_id, channel_scope=current_account.channel_scope,
                ).exists()):
            raise ValidationError("收款账户与余额所属账户不一致，请财务核实。")
        pool = _locked_pool(pool_scope)
        fee_reserve = settings.HUIFU_INVITE_REWARD_PAYOUT_FEE_RESERVE_CENTS
        if (pool.generation != generation or time.monotonic() - started > 30
                or balance["available_amount"] < intent.amount + fee_reserve):
            raise ValidationError("平台出款账户可用资金尚未确认或不足。")
        payout = ProviderInvitePayout.objects.create(
            reward=current, pool=pool, req_date=intent.req_date, req_seq_id=intent.req_seq_id,
            snapshot={"intent": asdict(intent), "scope": _scope(config), "receiver_profile": receiver.pk,
                      "receiver_scope": current_account.channel_scope, "fee_reserve": fee_reserve,
                      "balance_digest": balance["response_digest"]},
        )
        pool.generation += 1
        pool.save(update_fields=("generation", "updated_at"))
        current.payout_blocker = ""
        current.save(update_fields=("payout_blocker",))

    try:
        result = gateway.pay(intent)
    except DistributionEvidenceConflict as exc:
        result = {"status": "unknown", "conflict": True, "response_digest": exc.response_digest}
    except Exception:
        result = {"status": "unknown"}
    _observe(payout.pk, result, queried=False)
    return True


def query_reward(payout):
    config = HuifuPaymentConfig.from_settings()
    snapshot = payout.snapshot
    intent = BalancePaymentIntent(**snapshot["intent"])
    if (snapshot["scope"] != _scope(config) or intent.req_date != payout.req_date
            or intent.req_seq_id != payout.req_seq_id):
        raise ValidationError("奖励的原付款渠道或流水无法核实。")
    try:
        result = HuifuBalancePaymentGateway(config).query(intent, known_trade_no=payout.gateway_trade_no)
    except DistributionEvidenceConflict as exc:
        result = {"status": "unknown", "conflict": True, "response_digest": exc.response_digest}
    _observe(payout.pk, result, queried=True)


@transaction.atomic
def _observe(payout_id, result, *, queried):
    reward_id = ProviderInvitePayout.objects.values_list("reward_id", flat=True).get(pk=payout_id)
    reward = ProviderInviteReward.objects.select_for_update().get(pk=reward_id)
    payout = ProviderInvitePayout.objects.select_for_update().get(pk=payout_id)
    if payout.funds_released or payout.status == ProviderInvitePayout.Status.REVIEW:
        return
    status = result["status"]
    payout.response_digest = result.get("response_digest", "")
    if queried:
        payout.last_queried_at = timezone.now()
    # A sync response can arrive after a query. Never downgrade a known processing state.
    if queried or payout.status == "submitting":
        payout.status = status if status in {"processing", "failed"} and queried else (
            "processing" if status == "processing" else "unknown"
        )
    if queried and status == "succeeded":
        snap = payout.snapshot
        amount = snap["intent"]["amount"]
        fee = result["channel_fee_amount"]
        if amount != reward.amount or reward.status != "approved" or fee > snap["fee_reserve"] or result.get("conflict"):
            result["conflict"] = True
        else:
            wallet = locked_wallet(
                snap["receiver_profile"], scope=snap["receiver_scope"], receiver_id=snap["intent"]["receiver_id"],
            )
            append_entry(wallet, kind="credit", amount=amount, source_key=f"invite-reward:{reward.pk}")
            payout.status = "succeeded"
            payout.gateway_trade_no = result["gateway_trade_no"]
            payout.fee_amount = fee
            reward.status = "paid"
            reward.paid_at = timezone.now()
            reward.transfer_reference = payout.gateway_trade_no
            reward.revision += 1
            reward.save(update_fields=("status", "paid_at", "transfer_reference", "revision"))
    if result.get("conflict") or payout.status in {"succeeded", "failed"}:
        pool = ProviderBalancePaymentPool.objects.select_for_update().get(pk=payout.pool_id)
        if result.get("conflict"):
            pool.hold_reason = "邀请奖励划款证据存在差异，请核对原流水。"
            payout.status = ProviderInvitePayout.Status.REVIEW
            reward.payout_blocker = "汇付付款证据不一致，请核对原流水。"
            reward.save(update_fields=("payout_blocker",))
        else:
            payout.funds_released = True
        pool.generation += 1
        pool.save()
    payout.save()
