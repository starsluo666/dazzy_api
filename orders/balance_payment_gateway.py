"""Huifu account payment protocol, NOT an automatic settlement executor.

Official contracts: yuer/api_acctpayzf.md (2026-09-23) and
yuer/api_acctpaycx.md (2026-09-21). Supports one recipient, ordinary HUIFU
non-delayed payment, platform-paid fees and IP-based risk data only.

The wallet_distributions executor enables submission only after committing a
unique intent and funding reservation. This gateway stays disabled by default.
Never use a local wallet debit as channel-funds evidence, or resubmit an unknown pay.
"""

from dataclasses import dataclass
from datetime import datetime
from ipaddress import ip_address
import json
import re
import uuid

from django.utils import timezone

from .distribution_gateway import json_object, money
from .distribution_transport import (
    DistributionEvidenceConflict, DistributionPreflightError, DistributionUncertain,
    sdk_call, validate_transport,
)
from .huifu import _canonical_digest, cents_to_yuan


@dataclass(frozen=True)
class BalancePaymentIntent:
    # These values come from the committed, immutable outbox in the executor.
    req_date: str
    req_seq_id: str
    merchant_id: str
    receiver_id: str
    amount: int  # Provider's entitlement in cents, not the whole customer bill.
    transfer_type: str  # Confirm actual business purpose with Huifu; no default.
    risk_ip: str  # Actual risk source, never a sample/loopback fallback.
    fee_mode: str  # merchant_payer / request_payer; must match merchant setup.
    out_acct_id: str = ""
    receiver_acct_id: str = ""


def _identifier(value, maximum):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,%d}" % maximum, value))


def payment_payload(intent):
    """Pure contract validation/building. No settings, database or network I/O."""
    try:
        valid_date = (
            isinstance(intent.req_date, str)
            and bool(re.fullmatch(r"[0-9]{8}", intent.req_date))
            and datetime.strptime(intent.req_date, "%Y%m%d")
        )
        address = ip_address(intent.risk_ip)
        valid_ip = (
            isinstance(intent.risk_ip, str) and len(intent.risk_ip) <= 32
            and not (address.is_loopback or address.is_unspecified or address.is_multicast)
        )
    except (TypeError, ValueError):
        valid_date = valid_ip = False
    if not (
        valid_date and valid_ip
        and _identifier(intent.req_seq_id, 128)
        and _identifier(intent.merchant_id, 32)
        and _identifier(intent.receiver_id, 32)
        and intent.receiver_id != intent.merchant_id
        and type(intent.amount) is int and 0 < intent.amount < 10**13
        and intent.transfer_type in ("01", "02", "03", "04", "05", "06")
        and intent.fee_mode in ("merchant_payer", "request_payer")
        and all(value == "" or _identifier(value, 32) for value in (
            intent.out_acct_id, intent.receiver_acct_id,
        ))
    ):
        raise DistributionPreflightError("account_payment_intent", "余额支付请求资料不完整或无效。")
    receiver = {"huifu_id": intent.receiver_id, "div_amt": cents_to_yuan(intent.amount)}
    if intent.receiver_acct_id:
        receiver["acct_id"] = intent.receiver_acct_id
    payload = {
        "req_date": intent.req_date,
        "req_seq_id": intent.req_seq_id,
        # huifu_id is deprecated on PAY. QUERY still requires the initiator ID.
        "out_huifu_id": intent.merchant_id,
        "ord_amt": cents_to_yuan(intent.amount),
        "acct_channel": "HUIFU",
        "delay_acct_flag": "N",
        "acct_split_bunch": json.dumps({"acct_infos": [receiver]}, separators=(",", ":")),
        "risk_check_data": json.dumps({
            "transfer_type": intent.transfer_type, "ip_addr": intent.risk_ip,
        }, separators=(",", ":")),
    }
    if intent.out_acct_id:
        payload["out_acct_id"] = intent.out_acct_id
    if intent.fee_mode == "request_payer":
        payload["trans_fee_take_flag"] = "OUT"
    # No fabricated SMS/LV3 callback, ZXE fund type, percentage or delayed split.
    return payload


class HuifuBalancePaymentGateway:
    def __init__(self, config, *, allow_submit=False):
        validate_transport(config)
        self.config = config
        self.allow_submit = allow_submit is True

    def verify_source(self, source, *, acct_id):
        """Independently query an original recharge/external payment, never repay it."""
        data = sdk_call("payment_query", {
            "huifu_id": self.config.merchant_id, "req_date": source["req_date"],
            "req_seq_id": source["req_seq_id"],
        }, self.config)
        expected = {
            "resp_code": "00000000", "huifu_id": self.config.merchant_id,
            "req_date": source["req_date"], "req_seq_id": source["req_seq_id"],
            "hf_seq_id": source["gateway_trade_no"], "trans_stat": "S",
            "delay_acct_flag": "N", "div_flag": "N",
            "acct_id": acct_id, "acct_stat": "S",
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise DistributionPreflightError("wallet_source", "充值或外部付款的渠道状态尚未核实。")
        if money(data.get("trans_amt")) != source["amount"]:
            raise DistributionPreflightError("wallet_source", "充值或外部付款金额不一致。")
        if data.get("fund_freeze_stat") not in (None, "", "UNFREEZE"):
            raise DistributionPreflightError("wallet_source", "充值或外部付款存在渠道冻结信息。")
        return _canonical_digest(data)

    def platform_balance(self, acct_id):
        """Only the configured, normal basic account; no cached or inferred funds."""
        from providers.huifu_user_transport import _unique_object, _reject_constant

        date, seq = timezone.localdate().strftime("%Y%m%d"), uuid.uuid4().hex
        data = sdk_call("balance", {
            "req_date": date, "req_seq_id": seq, "huifu_id": self.config.merchant_id,
        }, self.config)
        try:
            if data.get("resp_code") != "00000000" or data.get("req_date") != date or data.get("req_seq_id") != seq:
                raise ValueError
            rows = json.loads(data.get("acctInfo_list"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError
            matches = [row for row in rows if row.get("acct_id") == acct_id]
            if len(matches) != 1:
                raise ValueError
            row = matches[0]
            if row.get("huifu_id") != self.config.merchant_id or row.get("acct_type") != "01" or row.get("acct_stat") != "N":
                raise ValueError
            amounts = []
            for key in ("avl_bal", "frz_bal", "balance_amt"):
                value = row.get(key)
                if not isinstance(value, str) or len(value) > 16 or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", value):
                    raise ValueError
                whole, cents = value.split(".")
                amounts.append(int(whole) * 100 + int(cents))
            if amounts[0] + amounts[1] != amounts[2]:
                raise ValueError
        except (TypeError, ValueError):
            raise DistributionPreflightError("wallet_funds", "平台出款账户可用资金尚未核实。") from None
        return {"available_amount": amounts[0], "response_digest": _canonical_digest(data)}

    def _validate_intent(self, intent):
        payload = payment_payload(intent)
        if intent.merchant_id != self.config.merchant_id:
            raise DistributionPreflightError("account_payment_merchant", "余额支付出款商户不匹配。")
        return payload

    def pay(self, intent):
        if not self.allow_submit:
            raise DistributionPreflightError("account_payment_disabled", "余额支付尚未开放执行。")
        data = sdk_call("account_pay", self._validate_intent(intent), self.config)
        # Even an S response is only an observation. A signed independent query
        # must confirm the exact persisted payment before any provider credit.
        result = self._result(data)
        self._identity(data, intent, result, query=False)
        if "ord_amt" in data:
            self._amount(data["ord_amt"], intent.amount, result)
        if data.get("resp_code") in ("00000000", "00000100") and data.get("trans_stat") in ("P", "S", "F"):
            result["status"] = "processing"
        return result

    def query(self, intent, *, known_trade_no=""):
        self._validate_intent(intent)
        data = sdk_call("account_pay_query", {
            "huifu_id": intent.merchant_id,
            "org_req_date": intent.req_date,
            "org_req_seq_id": intent.req_seq_id,
        }, self.config)
        result = self._result(data)
        self._identity(data, intent, result, query=True)
        if "ord_amt" in data:
            self._amount(data["ord_amt"], intent.amount, result)
        trade_no = data.get("org_hf_seq_id")
        if known_trade_no and trade_no and known_trade_no != trade_no:
            self._conflict("account_query_trade_conflict", result)
        # Not found, request errors and system errors NEVER permit a new payment.
        if data["resp_code"] != "00000000":
            return result
        self._amount(data.get("ord_amt"), intent.amount, result)
        if data.get("delay_acct_flag", "N") != "N":
            self._conflict("account_query_delay_conflict", result)
        flag = data.get("trans_fee_take_flag")
        if flag not in (None, "", "OUT"):
            self._conflict("account_query_fee_bearer_conflict", result)
        if "unconfirm_amt" in data:
            self._amount(data["unconfirm_amt"], 0, result, code="account_query_delay_conflict")

        split = data.get("acct_split_bunch")
        if split not in (None, ""):
            split = json_object(split)
            rows = split.get("acct_infos")
            if not isinstance(rows, list):
                raise DistributionUncertain("余额支付收款明细缺失。")
            if len(rows) != 1:
                self._conflict("account_query_receiver_conflict", result)
            row = rows[0]
            if not isinstance(row, dict):
                raise DistributionUncertain("余额支付收款明细无法核实。")
            if row.get("huifu_id") != intent.receiver_id:
                if row.get("huifu_id"):
                    self._conflict("account_query_receiver_conflict", result)
                raise DistributionUncertain("余额支付收款方缺失。")
            self._amount(row.get("div_amt"), intent.amount, result)
            # Query's nested table does not guarantee acct_id. Check if supplied.
            if intent.receiver_acct_id and row.get("acct_id") not in (None, "", intent.receiver_acct_id):
                self._conflict("account_query_receiver_conflict", result)
            if row.get("trans_stat") not in (None, "", "S") and data.get("trans_stat") == "S":
                self._conflict("account_query_receiver_state_conflict", result)
            if "unconfirm_amt" in row:
                self._amount(row["unconfirm_amt"], 0, result, code="account_query_delay_conflict")
            # Per-row fee is returned only when the recipient pays. Its absence
            # is normal under OUT; a positive value contradicts our fee policy.
            if "fee_amt" in row:
                self._amount(row["fee_amt"], 0, result, code="account_query_fee_bearer_conflict")
            if split.get("fee_huifu_id") not in (None, "", intent.merchant_id):
                self._conflict("account_query_fee_bearer_conflict", result)

        state = data.get("trans_stat")
        if state in ("P", "F"):
            result["status"] = "processing" if state == "P" else "failed"
            return result
        # C is special flexible-employment completion, NOT ordinary success.
        if state != "S":
            return result
        if not split:
            raise DistributionUncertain("余额支付收款明细缺失。")
        fee = money(split.get("fee_amt"))  # Unknown fee is NOT zero.
        if fee and split.get("fee_huifu_id") != intent.merchant_id:
            raise DistributionUncertain("余额支付手续费承担方尚未核实。")
        if intent.fee_mode == "request_payer" and flag != "OUT":
            raise DistributionUncertain("余额支付手续费模式尚未核实。")
        if not isinstance(trade_no, str) or not trade_no.strip() or len(trade_no) > 128:
            raise DistributionUncertain("余额支付渠道流水缺失。")
        result.update(status="succeeded", gateway_trade_no=trade_no, channel_fee_amount=fee)
        return result

    @staticmethod
    def _result(data):
        return {
            "status": "unknown", "response_code": data["resp_code"],
            "response_digest": _canonical_digest(data),
            "gateway_trade_no": "", "channel_fee_amount": None,
        }

    @staticmethod
    def _conflict(code, result):
        raise DistributionEvidenceConflict(
            code, response_code=result["response_code"], response_digest=result["response_digest"],
        )

    @classmethod
    def _amount(cls, value, expected, result, *, code="account_query_amount_conflict"):
        if money(value) != expected:
            cls._conflict(code, result)

    @classmethod
    def _identity(cls, data, intent, result, *, query):
        expected = {
            "org_req_date" if query else "req_date": intent.req_date,
            "org_req_seq_id" if query else "req_seq_id": intent.req_seq_id,
            "huifu_id": intent.merchant_id,
            "out_huifu_id": intent.merchant_id,
        }
        missing = False
        for key, value in expected.items():
            if data.get(key) in (None, ""):
                missing = True
            elif data[key] != value:
                cls._conflict("account_query_identity_conflict", result)
        if missing:
            raise DistributionUncertain("余额支付流水或出款主体缺失，请查询原流水。")
