"""Verified manual-cash readiness; never infer disabled auto-settlement from omission."""

from datetime import timedelta
import re

from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import ValidationError

from .huifu_user import decode_field, digest
from .models import ProviderReceivingAccount
from .receiving_accounts import encrypt_details


class CashAccountVerificationExpired(ValidationError):
    """Internal preflight signal, before any payment request is registered/sent."""

    def __init__(self, provider_id):
        self.provider_id = provider_id
        super().__init__("收款账户核验已过期，需要重新查询渠道状态。")


@sensitive_variables()
def verify_cash_configuration(account, response, details, expected):
    # Optional/missing groups do not prove absence. Explicit [] or all status=0 do.
    settlements = decode_field(response, "settle_config_list", list)
    disabled = all(isinstance(row, dict) and row.get("settle_status") == "0" for row in settlements)
    cash = decode_field(response, "qry_cash_config_list", list)
    cards = decode_field(response, "qry_cash_card_info_list", list)
    mapping = {
        "out_fee_flag": "out_cash_flag",
        "out_fee_huifu_id": "out_cash_huifuid",
        "out_fee_acct_type": "out_cash_acct_type",
    }
    matching = [
        row
        for row in cash
        if isinstance(row, dict)
        and row.get("switch_state") == "1"
        and all(row.get(mapping.get(key, key)) == value for key, value in expected.items())
    ]
    candidates = [
        row
        for row in cards
        if isinstance(row, dict)
        and row.get("status") == "N"
        and row.get("card_type") == "1"
        and row.get("card_name") == details["real_name"]
        and row.get("card_no") == details["bank_card_number"]
        and row.get("prov_id") == details["bank_province_code"]
        and row.get("area_id") == details["bank_city_code"]
        and isinstance(row.get("token_no"), str)
        and re.fullmatch(r"[0-9A-Za-z]{1,20}", row["token_no"])
    ]
    account.automatic_settlement_disabled = disabled
    account.card_status = "S" if len(candidates) == 1 else "F"
    account.cash_status = "S" if expected and len(matching) == 1 else "F"
    account.verified_cash_config = expected if account.cash_status == "S" else {}
    account.cash_card_ciphertext = (
        encrypt_details(account.provider_id, {"token_no": candidates[0]["token_no"]})
        if len(candidates) == 1
        else ""
    )
    return disabled and account.card_status == "S" and account.cash_status == "S"


def manual_cash_account(provider_id, config, *, now=None, lock=False, require_fresh=True):
    """Only read-only UI eligibility may skip age; money operations use the default."""
    from .receiving_onboarding import ONBOARDING_CONSENT_VERSION

    now = now or timezone.now()
    accounts = ProviderReceivingAccount.objects.select_related("provider")
    if lock:
        accounts = accounts.select_for_update(of=("self",))
    account = accounts.filter(provider_id=provider_id).first()
    scope = digest(["prod", config.sys_id, config.product_id, settings.HUIFU_USER_UPPER_ID.strip()])
    if not account or account.automatic_settlement_disabled is not True:
        raise ValidationError("尚未核验关闭自动结算，请联系平台切换为余额手动提现。")
    if (
        account.provider.status != "approved"
        or not account.provider.has_verified_identity
        or account.channel_status != "active"
        or account.card_status != "S"
        or account.cash_status != "S"
        or account.audit_status in {"P", "N"}
        or not account.cash_card_ciphertext
        or not account.channel_checked_at
        or account.channel_checked_at > now
        or not account.onboarding_consented_at
        or account.onboarding_consent_version != ONBOARDING_CONSENT_VERSION
        or not account.user_huifu_id
        or account.channel_scope != scope
        or account.user_huifu_id
        in {config.merchant_id, config.sys_id, settings.HUIFU_USER_UPPER_ID.strip()}
    ):
        raise ValidationError("本人银行卡、手动提现能力或开户授权尚未核验通过。")
    cash = account.verified_cash_config
    if (
        cash.get("out_fee_flag") != "1"
        or cash.get("out_fee_huifu_id") != config.merchant_id
        or cash.get("cash_type") not in {"T1", "D1"}
    ):
        raise ValidationError("提现手续费尚未核验为平台承担。")
    if require_fresh and account.channel_checked_at < now - timedelta(minutes=30):
        raise CashAccountVerificationExpired(provider_id)
    return account
