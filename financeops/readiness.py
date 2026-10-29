"""Read-only configuration checks; never authorize or send a channel request.

Only allowlisted labels, setting names, limits and timestamps leave this module.
Configured means local checks passed, not channel permission or funds acceptance.
"""
from dataclasses import replace
from ipaddress import ip_address
import re

from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from orders.distribution_transport import validate_transport
from orders.huifu import HuifuPaymentConfig
from orders.wallet_distributions import rollout_start
from reconciliation.services import ZONE, start_date
from .watchdog import health


def _probe(check):
    try:
        check()
    except Exception:
        # Validator errors may contain configuration. Never serialize them.
        return False
    return True


@sensitive_variables()
def _keys_valid(config):
    from Crypto.PublicKey import RSA
    from dg_sdk.core.rsa_utils import fill_private_key_marker, fill_public_key_marker

    private = RSA.import_key(fill_private_key_marker(config.private_key))
    public = RSA.import_key(fill_public_key_marker(config.public_key))
    if (not private.has_private() or public.has_private()
            or min(private.size_in_bits(), public.size_in_bits()) < 2048):
        raise ValueError


def _check(key, label, ok, help_text):
    return {"key": key, "label": label, "ok": bool(ok), "help": help_text}


def _flag(key, label):
    return _check(key, label, getattr(settings, key), f"请在服务端核对 {key}。")


def _positive(value):
    return type(value) is int and value > 0


def _group(key, label, switch, checks, *, limit=None):
    enabled = bool(getattr(settings, switch))
    return {
        "key": key, "label": label, "switch": switch, "enabled": enabled,
        "state": "disabled" if not enabled else (
            "configured" if all(item["ok"] for item in checks) else "incomplete"
        ),
        "limit_cents": limit if _positive(limit) else None, "checks": checks,
    }


def _risk_ip_valid():
    value = settings.HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP.strip()
    try:
        address = ip_address(value)
        return len(value) <= 32 and not (
            address.is_loopback or address.is_unspecified or address.is_multicast
        )
    except ValueError:
        return False


def _hosts_valid(hosts):
    if not isinstance(hosts, (list, tuple)) or not hosts:
        return False
    for host in hosts:
        if not isinstance(host, str) or len(host) > 253 or "." not in host:
            return False
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                   for part in host.split(".")):
            return False
        try:
            ip_address(host)
        except ValueError:
            continue
        return False
    return True


@sensitive_variables()
def readiness(now=None):
    now = now or timezone.now()
    config = HuifuPaymentConfig.from_settings()
    # Inspect configuration independently of the live switch. This immutable
    # copy is used only by local validators, never by SDK/network execution.
    local = replace(config, enabled=True)
    payment_valid = _probe(lambda: local.validate_for_payment(trade_type="T_JSAPI"))
    keys_valid = _probe(lambda: _keys_valid(local))
    transport_valid = _probe(lambda: validate_transport(local))

    def channel():
        return _check("HUIFU_PAYMENT_ENABLED", "收款通道及正式 SDK 配置",
                      config.enabled and transport_valid,
                      "请核对收款开关、正式环境、签名密钥、SDK 版本与日志配置。")

    def fees():
        return _flag("HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED", "平台承担手续费已登记确认")

    def upper():
        return _check("HUIFU_USER_UPPER_ID", "达人收款账户归属配置",
                      bool(settings.HUIFU_USER_UPPER_ID.strip()), "请配置已核实的开户归属。")

    def jobs():
        return _flag("HUIFU_PROVIDER_INCOME_JOBS_ENABLED", "自动收入与原流水查询任务已启用")

    def limit(key):
        return _check(key, "单笔限额", _positive(getattr(settings, key)),
                      "请配置大于零的单笔限额，单位为分。")

    split_limit = settings.HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS
    balance_limit = settings.HUIFU_PROVIDER_BALANCE_PAYMENT_MAX_CENTS
    cash_limit = settings.HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS
    reserve = settings.HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_RESERVE_CENTS
    bill_start = start_date()
    hosts = settings.HUIFU_RECONCILIATION_DOWNLOAD_HOSTS
    groups = [
        _group("payment", "微信收款", "HUIFU_PAYMENT_ENABLED", [
            _check("payment_parameters", "支付环境、商户、通知地址及微信配置", payment_valid,
                   "请核对支付基础参数、HTTPS 通知地址及公众号标识。"),
            _check("signing_keys", "签名密钥格式", keys_valid,
                   "请核对 HUIFU_RSA_PRIVATE_KEY 与 HUIFU_RSA_PUBLIC_KEY 的格式。"),
        ]),
        _group("distribution", "达人订单延时分账", "HUIFU_PROVIDER_DISTRIBUTION_ENABLED", [
            channel(), upper(), fees(), jobs(),
            _flag("HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED", "新订单延时支付已启用"),
            limit("HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS"),
        ], limit=split_limit),
        _group("balance", "余额／组合支付的达人结算", "HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED", [
            channel(), upper(), fees(), jobs(),
            _flag("HUIFU_PROVIDER_BALANCE_PAYMENT_CONFIRMED", "余额付款权限与用途已登记确认"),
            limit("HUIFU_PROVIDER_BALANCE_PAYMENT_MAX_CENTS"),
            _check("HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID", "平台付款账户格式",
                   bool(re.fullmatch(r"[A-Za-z0-9]{1,9}",
                                     settings.HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID.strip())),
                   "请配置经渠道核实的实际平台基本户。"),
            _check("HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_MODE", "手续费模式",
                   settings.HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_MODE.strip()
                   in ("merchant_payer", "request_payer"), "请配置经汇付确认的手续费模式。"),
            _check("HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_RESERVE_CENTS", "手续费预留",
                   type(reserve) is int and 0 <= reserve < 10**13,
                   "请核实费用上限后配置非负整数预留，单位为分。"),
            _check("HUIFU_PROVIDER_BALANCE_PAYMENT_TRANSFER_TYPE", "业务类型",
                   settings.HUIFU_PROVIDER_BALANCE_PAYMENT_TRANSFER_TYPE.strip()
                   in ("01", "02", "03", "04", "05", "06"), "请使用汇付确认的实际业务类型。"),
            _check("HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP", "风控来源格式", _risk_ip_valid(),
                   "请配置汇付认可的真实风控来源；本页不展示 IP。"),
            _check("HUIFU_PROVIDER_BALANCE_PAYMENT_START_AT", "带时区的启用时刻",
                   rollout_start() is not None, "请明确启用时刻；仅覆盖该时刻之后支付的新订单。"),
        ], limit=balance_limit),
        _group("withdrawal", "达人主动提现", "HUIFU_PROVIDER_WITHDRAWAL_ENABLED", [
            channel(), upper(), fees(), jobs(), limit("HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS"),
        ], limit=cash_limit),
        _group("reconciliation", "每日渠道对账", "HUIFU_RECONCILIATION_ENABLED", [
            channel(),
            _check("HUIFU_RECONCILIATION_START_DATE", "对账起始日期",
                   bill_start is not None and bill_start <= timezone.localtime(now, ZONE).date(),
                   "请配置有效且不晚于今天的对账起始日期。"),
            _check("HUIFU_RECONCILIATION_DOWNLOAD_HOSTS", "账单下载主机格式",
                   _hosts_valid(hosts), "请配置经核实的官方下载主机，不填写 URL 或通配符。"),
        ]),
    ]
    return {
        "checked_at": now,
        "environment": {"prod": "正式环境", "mertest": "联调环境"}.get(
            config.environment, "环境配置无效"
        ),
        "groups": groups, "jobs": health(now),
        "manual_checks": [
            {"key": "permissions", "label": "汇付权限与账户范围",
             "description": "向汇付核实分账、余额付款、主动提现及账单查询权限和账户范围。"},
            {"key": "fees", "label": "手续费、资金用途与风控资料",
             "description": "核对实际扣费账户、费用规则、充值资金用途与风控资料来源。"},
            {"key": "acceptance", "label": "真实小额闭环与银行到账",
             "description": "逐项验收充值、余额及组合支付、退款、分账、提现，并核对银行凭证和次日账单。"},
            {"key": "operations", "label": "通知可达与中断恢复",
             "description": "验收公网支付通知、漏通知查单补偿、任务中断恢复和账单缺失提醒。"},
        ],
    }
