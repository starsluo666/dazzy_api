"""Recharge consumption benefits. All mutations require the owning wallet lock."""
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from .models import UserWallet, WalletLotDebit

CONSUMPTION_PRICING_VERSION = "wallet-consumption-v2"


def best_discount_rate(user_id):
    wallet = UserWallet.objects.filter(user_id=user_id).first()
    return best_wallet_discount_rate(wallet) if wallet else 10000


def best_wallet_discount_rate(wallet):
    if not wallet.available_balance:
        return 10000
    return wallet.balance_lots.filter(available_amount__gt=0).values_list(
        "discount_rate_bps", flat=True
    ).first() or 10000


def benefits_payload(wallet):
    groups = list(wallet.balance_lots.filter(available_amount__gt=0).order_by(
        "discount_rate_bps"
    ).values("discount_rate_bps").annotate(available_amount=Sum("available_amount")))
    return {
        "discount_balances": groups,
        "ordinary_balance": max(wallet.available_balance - sum(item["available_amount"] for item in groups), 0),
        "best_discount_rate_bps": groups[0]["discount_rate_bps"] if groups and wallet.available_balance else 10000,
    }


def hold_lots(wallet, allocation):
    lots = list(wallet.balance_lots.filter(available_amount__gt=0))
    if sum(lot.available_amount for lot in lots) > wallet.available_balance:
        raise ValidationError({"wallet": "钱包批次余额不一致，请联系客服核对。"})
    remaining = allocation.wallet_amount
    for lot in lots:
        amount = min(remaining, lot.available_amount)
        if not amount:
            break
        WalletLotDebit.objects.create(allocation=allocation, lot=lot, amount=amount)
        lot.available_amount -= amount
        lot.save(update_fields=("available_amount",))
        remaining -= amount
    # The remainder belongs to ordinary/legacy balance, not a new benefit lot.


def restore_lots(wallet, allocation, amount):
    """Restore in original debit order, capped per batch; legacy remainder last."""
    remaining = amount
    for debit in allocation.lot_debits.select_related("lot").order_by("id"):
        restored = min(remaining, debit.amount - debit.restored_amount)
        if not restored:
            continue
        lot = debit.lot
        if lot.wallet_id != wallet.pk or lot.available_amount + restored > lot.credited_amount:
            raise ValidationError({"wallet": "钱包批次退款不一致，请联系客服核对。"})
        lot.available_amount += restored
        lot.save(update_fields=("available_amount",))
        debit.restored_amount += restored
        debit.save(update_fields=("restored_amount",))
        remaining -= restored
        if not remaining:
            break
