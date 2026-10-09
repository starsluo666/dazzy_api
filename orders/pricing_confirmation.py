"""A displayed quote is consent, not client-authoritative payment pricing."""
from django.core import signing
from rest_framework.exceptions import ValidationError

SALT = "provider-order-consumption-quote-v2"


def terms(user_id, data, quote):
    return {
        "user": user_id, "service": data["service"].pk,
        "starts_at": data["starts_at"].isoformat(), "duration": data["duration_minutes"],
        "address": data["address_id"], "coupon": str(data.get("coupon_id") or ""),
        "service_amount": quote.service_fee_amount, "travel": quote.transport_fee_amount,
        "coupon_discount": quote.snapshot["coupon_discount_amount"],
        "rate": quote.snapshot["wallet_discount_rate_bps"], "payable": quote.payable_amount,
    }


def sign_quote(user_id, data, quote):
    return signing.dumps(terms(user_id, data, quote), salt=SALT, compress=True)


def check_quote(user_id, data, quote):
    token = data.get("pricing_token")
    # Old clients without consumption benefits remain compatible. New clients
    # always send the token; discount-eligible orders must explicitly confirm it.
    if not token and quote.snapshot["wallet_discount_rate_bps"] == 10000:
        return
    try:
        confirmed = signing.loads(token or "", salt=SALT, max_age=300)
    except signing.BadSignature:
        raise ValidationError({"pricing_token": "计价已过期，请刷新并确认订单金额。"}) from None
    if confirmed != terms(user_id, data, quote):
        raise ValidationError({"pricing_token": "余额折扣或订单金额已变化，请刷新后重新确认。"})
