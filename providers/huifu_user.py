"""Personal LV1 user + own debit-card settlement, not a merchant or a transfer API."""
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlsplit

from Crypto.PublicKey import RSA
from django.conf import settings
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import APIException

from orders.huifu import _normalise_pem


class OnboardingUnavailable(APIException):
    status_code = 503
    default_detail = "平台开户配置尚未完成，请联系客服。已保存资料不会自动提交。"


class ChannelUncertain(Exception):
    """No reliable terminal result. Must NOT start another registration."""


def decode_field(data, key, kind, *, optional=False):
    value = data.get(key)
    if value in (None, "") and optional:
        return kind()
    if not isinstance(value, str):
        raise ChannelUncertain("Invalid channel JSON field")
    try:
        value = json.loads(value)
    except (ValueError, TypeError):
        raise ChannelUncertain("Invalid channel JSON field") from None
    if not isinstance(value, kind):
        raise ChannelUncertain("Invalid channel JSON type")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def settlement_config(raw):
    """Restricted P0 bank settlement contract. No invented rate/batch defaults."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
        required = {"settle_cycle", "settle_pattern", "settle_batch_no", "workday_fixed_ratio", "workday_constant_amt", "out_settle_flag"}
        allowed = required | {"fixed_ratio", "constant_amt", "min_amt", "remained_amt", "is_priority_receipt", "out_settle_huifuid", "out_settle_acct_type"}
        if not required <= value.keys() or value.keys() - allowed:
            raise ValueError
        if any(not isinstance(v, str) for v in value.values()):
            raise ValueError
        if value["settle_cycle"] not in {"T1", "D1", "TS"} or value["settle_pattern"] != "P0":
            raise ValueError
        # Official batch table: 0, 100, ... 2300. Explicit, never choose for operations.
        if value["settle_batch_no"] not in {str(hour * 100) for hour in range(24)}:
            raise ValueError
        if value.get("is_priority_receipt", "N") not in {"Y", "N"}:
            raise ValueError
        if value["out_settle_flag"] not in {"1", "2"}:
            raise ValueError
        if value["out_settle_flag"] == "1":
            if not re.fullmatch(r"[0-9]{1,18}", value.get("out_settle_huifuid", "")) or value.get("out_settle_acct_type") not in {"01", "02", "05"}:
                raise ValueError
        if value["settle_cycle"] == "D1" and not {"fixed_ratio", "constant_amt"} <= value.keys():
            raise ValueError
        for key in ("fixed_ratio", "workday_fixed_ratio", "constant_amt", "workday_constant_amt", "min_amt", "remained_amt"):
            if key not in value:
                continue
            item = value[key]
            limit = 6 if "ratio" in key else 14 if key in {"min_amt", "remained_amt"} else 15
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", item) or len(item) > limit:
                raise ValueError
            if "ratio" in key and Decimal(item) > 100:
                raise ValueError
            if key in {"min_amt", "remained_amt"} and Decimal(item) < Decimal("0.01"):
                raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise OnboardingUnavailable("银行卡结算参数尚未完成配置，请联系平台。") from None


def cash_config(raw):
    """Explicit T1/D1 pilot; platform pays all withdrawal fees externally."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
        required = {"cash_type", "out_fee_flag", "out_fee_huifu_id", "out_fee_acct_type"}
        amounts = {"fix_amt", "fee_rate", "weekday_fix_amt", "weekday_fee_rate"}
        if not required <= value.keys() or value.keys() - (required | amounts):
            raise ValueError
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError
        if value["cash_type"] not in {"T1", "D1"} or not {"fix_amt", "fee_rate"} & value.keys():
            raise ValueError
        if value["out_fee_flag"] != "1" or value["out_fee_huifu_id"] != settings.HUIFU_MERCHANT_ID.strip():
            raise ValueError
        if not re.fullmatch(r"[0-9]{1,18}", value["out_fee_huifu_id"]) or value["out_fee_acct_type"] not in {"01", "02", "05"}:
            raise ValueError
        for key in amounts & value.keys():
            item = value[key]
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", item) or len(item) > 6:
                raise ValueError
            if "rate" in key and Decimal(item) > 100:
                raise ValueError
            if key.startswith("weekday") and value["cash_type"] != "D1":
                raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise OnboardingUnavailable("手动提现参数及平台承担手续费配置尚未完成，请联系平台。") from None


@dataclass(frozen=True)
class UserChannelConfig:
    sys_id: str
    product_id: str
    upper_id: str
    private_key: str = field(repr=False)
    public_key: str = field(repr=False)
    notify_url: str
    skill_source: str
    settlement: dict

    @property
    def scope(self):
        return digest(["prod", self.sys_id, self.product_id, self.upper_id])

    @classmethod
    @sensitive_variables()
    def load(cls, *, for_submission=False):
        # User onboarding has no documented local sandbox/mertest endpoint.
        if settings.HUIFU_ENV != "prod":
            raise OnboardingUnavailable("达人开户仅可在配置完成的正式渠道启用，本地沙箱不支持真实开户。")
        if for_submission and not settings.HUIFU_USER_ONBOARDING_ENABLED:
            raise OnboardingUnavailable()
        values = dict(
            sys_id=settings.HUIFU_SYS_ID.strip(), product_id=settings.HUIFU_PRODUCT_ID.strip(),
            upper_id=settings.HUIFU_USER_UPPER_ID.strip(),
            private_key=_normalise_pem(settings.HUIFU_RSA_PRIVATE_KEY),
            public_key=_normalise_pem(settings.HUIFU_RSA_PUBLIC_KEY),
            notify_url=settings.HUIFU_USER_NOTIFY_URL.strip(),
            skill_source=settings.HUIFU_USER_SKILL_SOURCE.strip(),
        )
        if any(not value for value in values.values()):
            raise OnboardingUnavailable()
        try:
            url = urlsplit(values["notify_url"])
        except ValueError:
            raise OnboardingUnavailable() from None
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or len(values["notify_url"]) > 128:
            raise OnboardingUnavailable()
        if len(values["sys_id"]) > 32 or len(values["product_id"]) > 32 or not re.fullmatch(r"[0-9]{1,18}", values["upper_id"]):
            raise OnboardingUnavailable()
        # Validate before recording an outbound attempt. The SDK otherwise swallows
        # request-signing errors and may send an empty signature.
        try:
            from dg_sdk.core.rsa_utils import fill_private_key_marker, fill_public_key_marker
            private = RSA.import_key(fill_private_key_marker(values["private_key"]))
            public = RSA.import_key(fill_public_key_marker(values["public_key"]))
            if not private.has_private() or public.has_private() or min(private.size_in_bits(), public.size_in_bits()) < 2048:
                raise ValueError
        except (ValueError, TypeError, IndexError):
            raise OnboardingUnavailable("平台开户密钥配置无效，请联系平台。") from None
        from backoffice.receiving_settings import effective_cash_config
        values["settlement"] = effective_cash_config() if for_submission else {}
        from .huifu_user_transport import ensure_transport_ready
        ensure_transport_ready()
        return cls(**values)


def channel_available():
    try:
        UserChannelConfig.load(for_submission=True)
        return True
    except OnboardingUnavailable:
        return False


def cert_fields(details):
    result = {
        "cert_type": "00", "cert_no": details["id_number"],
        "cert_validity_type": "1" if details["cert_long_term"] else "0",
        "cert_begin_date": details["cert_begin_date"].replace("-", ""),
    }
    if not details["cert_long_term"]:
        result["cert_end_date"] = details["cert_end_date"].replace("-", "")
    return result


@sensitive_variables()
def registration_payload(attempt, details):
    return {"req_seq_id": attempt.req_seq_id, "req_date": attempt.req_date,
            "name": details["real_name"], "mobile_no": details["mobile"], **cert_fields(details)}


@sensitive_variables()
def business_payload(attempt, account, details, config):
    card = {"card_type": "1", "card_name": details["real_name"], "card_no": details["bank_card_number"],
            "prov_id": details["bank_province_code"], "area_id": details["bank_city_code"],
            "mp": details["mobile"], "is_settle_default": "Y", **cert_fields(details)}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"req_seq_id": attempt.req_seq_id, "req_date": attempt.req_date,
            "huifu_id": account.user_huifu_id, "upper_huifu_id": config.upper_id,
            "account_level": "LV1", "card_info": encode(card),
            "cash_config": encode([cash_config(attempt.settlement_config)]), "async_return_url": config.notify_url}


class HuifuUserGateway:
    def __init__(self, config):
        self.config = config

    @sensitive_variables()
    def call(self, kind, payload):
        from dg_sdk import (
            V2UserBasicdataIndvRequest, V2UserBusiOpenRequest,
            V2UserBasicdataQueryRequest, V2UserListQueryRequest,
        )
        from .huifu_user_transport import sdk_call
        cls = {"register": V2UserBasicdataIndvRequest, "configure": V2UserBusiOpenRequest,
               "query": V2UserBasicdataQueryRequest, "recover": V2UserListQueryRequest}[kind]
        request = cls()
        extra = {}
        for key, value in payload.items():
            if hasattr(request, key):
                setattr(request, key, value)
            else:
                extra[key] = value
        return sdk_call(request, extra, self.config)
