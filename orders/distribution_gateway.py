"""Huifu delayed confirmation contract. All money is integer cents internally."""

import json
import re

from .distribution_transport import (
    DistributionEvidenceConflict, DistributionPreflightError, DistributionUncertain,
    sdk_call, validate_transport,
)
from .huifu import _canonical_digest, cents_to_yuan


def money(value):
    if (
        not isinstance(value, str)
        or len(value) > 14
        or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{2}", value)
    ):
        raise DistributionUncertain("渠道金额格式无法核实。")
    whole, fraction = value.split(".")
    return int(whole) * 100 + int(fraction)


def json_object(value):
    from providers.huifu_user_transport import _unique_object, _reject_constant

    try:
        if not isinstance(value, str):
            raise ValueError
        result = json.loads(
            value, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise DistributionUncertain("渠道 JSON 字段无法核实。") from None


class HuifuDistributionGateway:
    def __init__(self, config):
        validate_transport(config)
        self.config = config

    def verify_payment(self, snapshot):
        payment = snapshot["payment"]
        data = sdk_call(
            "payment_query",
            {
                "huifu_id": snapshot["merchant_id"],
                "req_date": payment["req_date"],
                "req_seq_id": payment["req_seq_id"],
            },
            self.config,
        )
        expected = {
            "resp_code": "00000000",
            "huifu_id": snapshot["merchant_id"],
            "req_date": payment["req_date"],
            "req_seq_id": payment["req_seq_id"],
            "hf_seq_id": payment["gateway_trade_no"],
            "trans_stat": "S",
            "delay_acct_flag": "Y",
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise DistributionPreflightError("payment_identity", "原支付交易、商户或延时状态无法核实。")
        if money(data.get("trans_amt")) != snapshot["paid_amount"]:
            raise DistributionPreflightError("payment_amount", "原支付金额不一致。")
        fee = json_object(data.get("payment_fee"))
        fee_flag = snapshot.get("fee_flag")
        if (
            fee_flag not in {"1", "2"}
            or fee.get("fee_flag") != fee_flag
            or fee.get("fee_huifu_id") != snapshot["merchant_id"]
        ):
            raise DistributionPreflightError("payment_fee_policy", "渠道支付手续费扣款方式或平台承担方与原支付快照不一致。")
        payment_fee_amount = money(fee.get("fee_amount"))
        # Preserve the gross business commission. Internal collection fees reduce
        # only the platform's split, never the provider's contracted entitlement.
        # External fees are paid separately and MUST NOT be deducted here again.
        platform_split_amount = snapshot["platform_amount"]
        if fee_flag == "2":
            platform_split_amount -= payment_fee_amount
            if platform_split_amount < 0:
                raise DistributionPreflightError("platform_fee_shortfall", "平台份额不足以承担支付手续费，禁止扣减达人收入，请人工核账。")
        split_amount = snapshot["provider_amount"] + platform_split_amount
        if split_amount <= 0 or money(data.get("unconfirm_amt")) != split_amount:
            raise DistributionPreflightError("available_amount", "渠道可分账金额与扣费后的分账总额不一致，请人工核账。")
        return {
            "payment_fee_amount": payment_fee_amount,
            "platform_split_amount": platform_split_amount,
            "split_amount": split_amount,
            "payment_query_digest": _canonical_digest(data),
        }

    def confirm(self, record):
        snapshot = record.snapshot
        data = sdk_call(
            "confirm",
            {
                "req_date": record.req_date,
                "req_seq_id": record.req_seq_id,
                "huifu_id": snapshot["merchant_id"],
                "org_req_date": snapshot["payment"]["req_date"],
                "org_req_seq_id": snapshot["payment"]["req_seq_id"],
                "acct_split_bunch": json.dumps(
                    {"acct_infos": snapshot["receivers"]}, separators=(",", ":")
                ),
            },
            self.config,
        )
        # A synchronous response is evidence, never sufficient to unlock refunds.
        # Success is confirmed by a separately identified, signed V3 query.
        expected = {
            "req_date": record.req_date,
            "req_seq_id": record.req_seq_id,
            "huifu_id": snapshot["merchant_id"],
            "org_req_date": snapshot["payment"]["req_date"],
            "org_req_seq_id": snapshot["payment"]["req_seq_id"],
        }
        if any(data.get(key) != value for key, value in expected.items()):
            raise DistributionUncertain()
        accepted = data.get("resp_code") in {"00000000", "00000100"} and data.get("trans_stat") in {
            "P",
            "S",
            "F",
        }
        return {
            "status": "processing" if accepted else "unknown",
            "response_code": data["resp_code"],
            "response_digest": _canonical_digest(data),
            "split_fee_amount": None,
            "gateway_trade_no": "",
        }

    def query(self, record):
        data = sdk_call(
            "confirm_query",
            {
                # V3 queries the CONFIRMATION request, NOT the original payment.
                "org_req_date": record.req_date,
                "org_req_seq_id": record.req_seq_id,
                "huifu_id": record.snapshot["merchant_id"],
            },
            self.config,
        )
        result = {
            "status": "unknown",
            "response_code": data["resp_code"],
            "response_digest": _canonical_digest(data),
            "split_fee_amount": None,
            "gateway_trade_no": "",
        }
        def conflict(code):
            # sdk_call already verified the signature. Persist only a digest/code.
            raise DistributionEvidenceConflict(
                code, response_code=result["response_code"],
                response_digest=result["response_digest"],
            )

        if (
            data.get("huifu_id") != record.snapshot["merchant_id"]
            or data.get("org_req_seq_id") != record.req_seq_id
        ):
            if data.get("huifu_id") and data.get("org_req_seq_id"):
                conflict("query_identity_conflict")
            raise DistributionUncertain()
        if "org_req_date" in data and data["org_req_date"] != record.req_date:
            conflict("query_identity_conflict")
        if data["resp_code"] != "00000000":
            return result  # Including 23000001 (not found): NEVER permission to resend.
        state = data.get("trans_stat")
        if state in {"P", "F"}:
            result["status"] = "processing" if state == "P" else "failed"
            return result
        if state != "S":
            return result
        # The official V3 prose calls this JSON-array but its nested table defines
        # an object with acct_infos. Accept that table contract only; unknown wire
        # shapes stop reconciliation rather than guessing a successful payout.
        rows = json_object(data.get("acct_split_bunch")).get("acct_infos")
        if not isinstance(rows, list):
            raise DistributionUncertain("渠道分账明细缺失或结构待确认。")
        if len(rows) != len(record.snapshot["receivers"]):
            conflict("query_receiver_conflict")
        expected = {row["huifu_id"]: money(row["div_amt"]) for row in record.snapshot["receivers"]}
        seen, total_fee, incomplete = set(), 0, False
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("huifu_id"), str):
                incomplete = True
                continue
            receiver = row["huifu_id"]
            if receiver in seen or receiver not in expected:
                conflict("query_amount_conflict")
            seen.add(receiver)
            try:
                amount = money(row.get("div_amt"))
            except DistributionUncertain:
                incomplete = True
            else:
                if amount != expected[receiver]:
                    conflict("query_amount_conflict")
        # A missing optional fee in one row must not hide a known amount or
        # receiver contradiction in another row. Inspect every provable fact.
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Missing fee is UNKNOWN, not zero. Requires real channel evidence.
            try:
                fee = money(row.get("split_fee_amt"))
            except DistributionUncertain:
                incomplete = True
                continue
            if fee and row.get("split_fee_huifu_id") != record.snapshot["merchant_id"]:
                if row.get("split_fee_huifu_id"):
                    conflict("query_fee_bearer_conflict")
                incomplete = True
            total_fee += fee
        trade_no = data.get("hf_seq_id")
        if record.gateway_trade_no and trade_no and trade_no != record.gateway_trade_no:
            conflict("query_trade_conflict")
        if incomplete:
            raise DistributionUncertain("渠道分账或费用明细不完整。")
        if not isinstance(trade_no, str) or not trade_no or len(trade_no) > 128:
            raise DistributionUncertain()
        result.update(status="succeeded", split_fee_amount=total_fee, gateway_trade_no=trade_no)
        return result


def receivers(provider_id, merchant_id, provider_amount, platform_amount):
    # Zero-valued split items violate the channel's minimum 0.01 requirement.
    return [
        {"huifu_id": receiver, "div_amt": cents_to_yuan(amount)}
        for receiver, amount in ((provider_id, provider_amount), (merchant_id, platform_amount))
        if amount > 0
    ]
