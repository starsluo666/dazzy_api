"""Verified manual-cash readiness; never infer disabled auto-settlement from omission."""

from datetime import timedelta
import re

from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import ValidationError

from .huifu_user import ChannelUncertain, decode_field, digest
from .models import ProviderReceivingAccount
from .receiving_accounts import encrypt_details


class CashAccountVerificationExpired(ValidationError):
    """Internal preflight signal, before any payment request is registered/sent."""

    def __init__(self, provider_id):
        self.provider_id = provider_id
        super().__init__("收款账户核验已过期，需要重新查询渠道状态。")


@sensitive_variables()
def _query_rows(response, key, label, issues):
    if response.get(key) in (None, ""):
        issues.append(f"渠道未返回{label}")
        return None
    try:
        rows = decode_field(response, key, list)
        if not all(isinstance(row, dict) for row in rows):
            raise ChannelUncertain()
        return rows
    except ChannelUncertain:
        # Whitelisted descriptions only: never echo raw responses or exceptions.
        issues.append(f"渠道返回的{label}格式异常")
        return None


@sensitive_variables()
def verify_cash_configuration(account, response, details, expected):
    """S=verified, F=confirmed unmet condition, blank/None=insufficient evidence.

    Updates independent checks and a safe diagnostic on the caller's locked account.
    Only a complete result may authorize the caller to mark the account active.
    """
    # Rebuild every check from this response; do not retain a stale S or default to F.
    account.automatic_settlement_disabled = None
    account.card_status = ""
    account.cash_status = ""
    account.verified_cash_config = {}
    account.cash_card_ciphertext = ""
    issues = []
    settlements = _query_rows(response, "settle_config_list", "自动结算配置", issues)
    if settlements is not None:
        if any(row.get("settle_status") == "1" for row in settlements):
            account.automatic_settlement_disabled = False
            issues.append("渠道自动结算仍开启，需平台联系汇付关闭")
        elif all(row.get("settle_status") == "0" for row in settlements):
            # Only explicit [] or every status=0 proves disabled auto-settlement.
            account.automatic_settlement_disabled = True
        else:
            issues.append("渠道自动结算状态缺失或无法识别")

    cash = _query_rows(response, "qry_cash_config_list", "手动提现配置", issues)
    mapping = {
        "out_fee_flag": "out_cash_flag",
        "out_fee_huifu_id": "out_cash_huifuid",
        "out_fee_acct_type": "out_cash_acct_type",
    }
    valid_expected = (
        isinstance(expected, dict)
        and expected.get("cash_type") in ("T1", "D1")
        and all(expected.get(key) for key in mapping)
        and bool(expected.get("fix_amt") or expected.get("fee_rate"))
    )
    if not valid_expected:
        issues.append("缺少有效的手动提现授权配置，请联系平台核实")
    elif cash is not None:
        fields = {mapping.get(key, key): value for key, value in expected.items()}
        relevant = [row for row in cash if row.get("cash_type") == expected["cash_type"]]
        if any(row.get("cash_type") not in ("T1", "D1", "D0") for row in cash) or any(
            row.get("switch_state") not in ("0", "1")
            or (row.get("switch_state") == "1" and any(
                not isinstance(row.get(key), str) or not row[key] for key in fields
            ))
            for row in relevant
        ):
            issues.append("渠道手动提现配置不完整，无法核对开关、费率及承担方")
        elif len(relevant) > 1:
            issues.append("渠道返回多条同周期提现配置，需平台核实")
        elif not relevant or relevant[0].get("switch_state") == "0":
            account.cash_status = "F"
            issues.append("渠道未开通所授权周期的手动提现")
        elif any(relevant[0][key] != value for key, value in fields.items()):
            account.cash_status = "F"
            issues.append("渠道提现费率或手续费承担方与授权配置不一致")
        else:
            account.cash_status = "S"
            account.verified_cash_config = expected

    cards = _query_rows(response, "qry_cash_card_info_list", "提现银行卡资料", issues)
    if cards is not None:
        card_fields = {
            "card_type": "1", "card_name": details["real_name"],
            "card_no": details["bank_card_number"],
            "prov_id": details["bank_province_code"], "area_id": details["bank_city_code"],
        }
        if any(
            row.get("status") not in ("N", "C")
            or any(not isinstance(row.get(key), str) or not row[key] for key in card_fields)
            or "*" in row.get("card_name", "") or "*" in row.get("card_no", "")
            for row in cards
        ):
            issues.append("渠道提现银行卡资料不完整或已脱敏，暂无法核验")
        else:
            candidates = [row for row in cards if row["status"] == "N" and all(
                row[key] == value for key, value in card_fields.items()
            )]
            if not candidates:
                account.card_status = "F"
                issues.append("渠道未找到与已保存资料一致的正常本人提现卡")
            elif len(candidates) != 1:
                issues.append("渠道返回多张匹配提现卡，需平台核实")
            elif not isinstance(candidates[0].get("token_no"), str) or not re.fullmatch(
                r"[0-9A-Za-z]{1,20}", candidates[0]["token_no"]
            ):
                issues.append("渠道未返回有效的提现卡标识")
            else:
                account.card_status = "S"
                account.cash_card_ciphertext = encrypt_details(
                    account.provider_id, {"token_no": candidates[0]["token_no"]},
                )
    account.channel_message = (
        "；".join(issues) + "。暂不可提现，请稍后刷新；持续异常请联系客服。"
        if issues else ""
    )
    return (
        account.automatic_settlement_disabled is True
        and account.card_status == "S" and account.cash_status == "S"
    )


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
