"""Platform fee policy and local arithmetic, not a Huifu fee instruction.

Fees must be reconciled from channel evidence before passing actual amounts here.
A missing fee is unknown, not free. Bank settlement fees may be batch-level and
must not be charged to every order without a reconciled allocation.
"""


def platform_fee_snapshot(
    *,
    provider_amount,
    platform_amount,
    payment_fee_amount=None,
    split_fee_amount=None,
    bank_settlement_fee_amount=None,
):
    amounts = {
        "provider_amount": provider_amount,
        "platform_amount": platform_amount,
        "payment_fee_amount": payment_fee_amount,
        "split_fee_amount": split_fee_amount,
        "bank_settlement_fee_amount": bank_settlement_fee_amount,
    }
    for name, amount in amounts.items():
        if amount is None and name.endswith("fee_amount"):
            continue
        if type(amount) is not int or amount < 0:
            raise ValueError(f"{name} must be a non-negative integer number of cents")
    fees = (payment_fee_amount, split_fee_amount, bank_settlement_fee_amount)
    total = sum(fees) if all(fee is not None for fee in fees) else None
    net = platform_amount - total if total is not None else None
    return {
        "version": "provider-platform-fees-v1",
        "bearer": "platform",
        "scope": ["payment", "split", "bank_settlement"],
        "provider_fee_amount": 0,
        "provider_receivable_amount": provider_amount,
        "platform_gross_amount": platform_amount,
        "payment_fee_amount": payment_fee_amount,
        "split_fee_amount": split_fee_amount,
        "bank_settlement_fee_amount": bank_settlement_fee_amount,
        "total_fee_amount": total,
        # Keep losses visible; never recover a shortfall from provider income.
        "platform_net_amount": net,
        "platform_shortfall_amount": max(-net, 0) if net is not None else None,
    }
