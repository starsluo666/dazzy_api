"""Local configuration only. Reading/publishing never contacts Huifu."""
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.db import DatabaseError

from providers.huifu_user import OnboardingUnavailable, UserChannelConfig, cash_config
from .models import ReceivingWithdrawalSetting


FEE_FIELDS = ("fix_amt", "fee_rate", "weekday_fix_amt", "weekday_fee_rate")
FORM_FIELDS = ("cash_type", "out_fee_acct_type", *FEE_FIELDS)


def form_values(setting):
    return {
        "cash_type": setting.cash_type,
        "out_fee_acct_type": setting.out_fee_acct_type,
        **{key: format(getattr(setting, key), ".2f") if getattr(setting, key) is not None else None for key in FEE_FIELDS},
    }


def build_cash_config(form, merchant_id):
    return cash_config({
        "cash_type": form["cash_type"], "out_fee_acct_type": form["out_fee_acct_type"],
        "out_fee_flag": "1", "out_fee_huifu_id": merchant_id,
        **{key: str(form[key]) for key in FEE_FIELDS if form.get(key) is not None},
    })


def current_setting():
    try:
        return ReceivingWithdrawalSetting.objects.filter(singleton_key="default").first()
    except DatabaseError:
        # DB errors/missing migration are NOT permission to fall back to old fees.
        raise OnboardingUnavailable("收款与提现配置暂时无法读取，请联系平台检查数据库迁移。") from None


def effective_cash_config():
    setting = current_setting()
    if setting is None:
        return cash_config(settings.HUIFU_USER_CASH_CONFIG)
    if setting.fee_bearer_id != settings.HUIFU_MERCHANT_ID.strip():
        raise OnboardingUnavailable("平台手续费承担商户已变更，请在后台重新确认收款与提现配置。")
    return build_cash_config(form_values(setting), setting.fee_bearer_id)


def notify_url_valid(value):
    try:
        url = urlsplit(value)
        return bool(url.scheme == "https" and url.hostname and not (
            url.username or url.password or url.query or url.fragment
        ) and len(value) <= 128)
    except ValueError:
        return False


def settings_payload(setting):
    source = "admin" if setting else "environment" if settings.HUIFU_USER_CASH_CONFIG.strip() else "unconfigured"
    form = form_values(setting) if setting else {
        "cash_type": "", "out_fee_acct_type": "", **{key: None for key in FEE_FIELDS},
    }
    cash_error = ""
    try:
        if setting:
            if setting.fee_bearer_id != settings.HUIFU_MERCHANT_ID.strip():
                raise OnboardingUnavailable("平台商户已变更，请核实费用后重新保存。")
            build_cash_config(form, setting.fee_bearer_id)
        else:
            env_value = cash_config(settings.HUIFU_USER_CASH_CONFIG)
            form = {key: env_value.get(key) for key in FORM_FIELDS}
    except OnboardingUnavailable as exc:
        cash_error = str(exc.detail)

    # Whitelisted statuses only. Do not serialize settings, keys, account IDs or
    # raw exception strings. This is a local check, not channel approval.
    checks = []

    def check(key, label, ok, missing):
        checks.append({"key": key, "label": label, "ok": bool(ok), "message": "已满足" if ok else missing})

    check("environment", "渠道环境", settings.HUIFU_ENV == "prod", "需由运维设置 HUIFU_ENV=prod")
    check("onboarding_switch", "渠道开户开关", settings.HUIFU_USER_ONBOARDING_ENABLED, "HUIFU_USER_ONBOARDING_ENABLED 尚未开启")
    for name, label in (("HUIFU_SYS_ID", "汇付系统号"), ("HUIFU_PRODUCT_ID", "汇付产品号")):
        check(name, label, 0 < len(getattr(settings, name).strip()) <= 32, f"请由运维配置 {name}")
    for name, label in (("HUIFU_MERCHANT_ID", "平台手续费承担商户"), ("HUIFU_USER_UPPER_ID", "用户上级汇付 ID")):
        check(name, label, re.fullmatch(r"[0-9]{1,18}", getattr(settings, name).strip()), f"请由运维配置有效的 {name}")
    check("HUIFU_USER_NOTIFY_URL", "开户回调地址", notify_url_valid(settings.HUIFU_USER_NOTIFY_URL.strip()), "请由运维配置有效 HTTPS 回调地址 HUIFU_USER_NOTIFY_URL")
    for name, label in (("HUIFU_RSA_PRIVATE_KEY", "平台签名私钥已填写"), ("HUIFU_RSA_PUBLIC_KEY", "汇付验签公钥已填写"), ("HUIFU_USER_SKILL_SOURCE", "接口版本来源")):
        check(name, label, bool(getattr(settings, name).strip()), f"请由运维配置 {name}")
    try:
        UserChannelConfig.load()  # Local key/SDK validation; no gateway instance.
        channel_valid = True
    except OnboardingUnavailable:
        channel_valid = False
    check("channel_validation", "渠道参数、密钥和 SDK 本地校验", channel_valid, "未通过，请核对服务器渠道参数、RSA 密钥格式和 SDK 版本")
    check("cash_config", "提现费用规则", not cash_error, cash_error)
    return {
        "form": form, "source": source, "revision": setting.revision if setting else 0,
        "updated_at": setting.updated_at.isoformat() if setting else None,
        "updated_by": (setting.updated_by.nickname or f"管理员 #{setting.updated_by_id}") if setting else None,
        "checks": checks, "onboarding_ready": all(item["ok"] for item in checks),
        "withdrawal_enabled": settings.HUIFU_PROVIDER_WITHDRAWAL_ENABLED,
    }
