"""Integer-cent, append-only earnings ledger. Caller locks wallet before mutation."""

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import ProviderProfile, ProviderIncomeWallet, ProviderIncomeEntry


def locked_wallet(provider_id, *, scope, receiver_id):
    ProviderProfile.objects.select_for_update().get(pk=provider_id)
    wallet, _ = ProviderIncomeWallet.objects.get_or_create(
        provider_id=provider_id,
        defaults={"channel_scope": scope, "receiver_id": receiver_id},
    )
    wallet = ProviderIncomeWallet.objects.select_for_update().get(pk=wallet.pk)
    if wallet.channel_scope != scope or wallet.receiver_id != receiver_id:
        raise ValidationError("收入余额所属渠道账户已变更，请联系平台核账。")
    return wallet


def append_entry(wallet, *, kind, amount, source_key, distribution=None, withdrawal=None):
    if ProviderIncomeEntry.objects.filter(source_key=source_key).exists():
        return
    if type(amount) is not int or amount <= 0:
        raise ValidationError("账务金额无效。")
    available, reserved = {
        "credit": (amount, 0),
        "reserve": (-amount, amount),
        "release": (amount, -amount),
        "paid": (0, -amount),
    }[kind]
    if wallet.available_amount + available < 0 or wallet.reserved_amount + reserved < 0:
        raise ValidationError("可用余额不足或提现冻结记录不一致。")
    ProviderIncomeEntry.objects.create(
        wallet=wallet,
        kind=kind,
        amount=amount,
        available_delta=available,
        reserved_delta=reserved,
        source_key=source_key,
        distribution=distribution,
        withdrawal=withdrawal,
    )
    wallet.available_amount += available
    wallet.reserved_amount += reserved
    if kind == "paid":
        wallet.paid_amount += amount
    wallet.save()


@transaction.atomic
def credit_distribution(record):
    snap = record.snapshot
    if (
        record.status != "succeeded"
        or record.attention_reason
        or snap.get("income_mode") != "manual_cash_v1"
    ):
        return  # Never backfill old/local-only/automatically settled money.
    amount = snap["provider_amount"]
    if amount == 0:
        return
    wallet = locked_wallet(
        snap["provider_id"], scope=snap["receiver_scope"], receiver_id=snap["receiver_id"]
    )
    append_entry(
        wallet,
        kind="credit",
        amount=amount,
        source_key=f"distribution:{record.pk}",
        distribution=record,
    )


def hold_distribution_wallet(record):
    ProviderProfile.objects.select_for_update().get(pk=record.settlement.provider_id)
    wallet = (
        ProviderIncomeWallet.objects.select_for_update()
        .filter(provider_id=record.settlement.provider_id)
        .first()
    )
    if wallet:
        wallet.hold_reason = "分账结果存在差异，请联系平台核账后再提现。"
        wallet.save()
