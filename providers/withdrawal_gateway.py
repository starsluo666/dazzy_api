"""Official SDK cash / cash-query / basic account balance contracts (2026-10-03).

Only a signed original-request query proves terminal status; a submit ACK never
does. No notify_url is sent: polling is the sole source of truth in this pilot.
"""

import json
import re
import uuid

from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from orders.distribution_gateway import money
from orders.distribution_transport import DistributionUncertain, sdk_call
from providers.huifu_user import digest


class HuifuWithdrawalGateway:
    def __init__(self, config):
        self.config = config

    def balance(self, receiver_id):
        date, seq = timezone.localdate().strftime("%Y%m%d"), uuid.uuid4().hex
        result = sdk_call(
            "balance", {"req_date": date, "req_seq_id": seq, "huifu_id": receiver_id}, self.config
        )
        if (
            result.get("resp_code") != "00000000"
            or result.get("req_date") != date
            or result.get("req_seq_id") != seq
        ):
            raise DistributionUncertain()
        raw = result.get("acctInfo_list")
        if not isinstance(raw, str):
            raise DistributionUncertain()
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError
            accounts = [
                row
                for row in items
                if isinstance(row, dict)
                and row.get("acct_type") == "01"
                and row.get("huifu_id") == receiver_id
            ]
            if (
                len(accounts) != 1
                or accounts[0].get("acct_stat") != "N"
                or not re.fullmatch(r"[A-Za-z0-9]{1,9}", accounts[0].get("acct_id", ""))
            ):
                raise ValueError
            row = accounts[0]
            available, frozen, total = (
                money(row.get("avl_bal")),
                money(row.get("frz_bal")),
                money(row.get("balance_amt")),
            )
            if total != available + frozen:
                raise ValueError
            return {
                "acct_id": row["acct_id"],
                "available_amount": available,
                "response_digest": digest(result),
            }
        except (ValueError, TypeError):
            raise DistributionUncertain() from None

    @sensitive_variables()
    def submit(self, record, token):
        snap = record.snapshot
        data = sdk_call(
            "cash",
            {
                "req_date": record.req_date,
                "req_seq_id": record.req_seq_id,
                "huifu_id": snap["receiver_id"],
                "acct_id": snap["acct_id"],
                "cash_amt": f"{record.amount // 100}.{record.amount % 100:02d}",
                "into_acct_date_type": snap["cash_type"],
                "token_no": token,
                "enchashment_channel": "00",
            },
            self.config,
        )
        if any(
            data.get(key) != value
            for key, value in {
                "req_date": record.req_date,
                "req_seq_id": record.req_seq_id,
                "huifu_id": snap["receiver_id"],
            }.items()
        ):
            raise DistributionUncertain()
        return {
            "status": "processing" if data.get("resp_code") == "00000000" else "unknown",
            "response_code": data["resp_code"],
            "response_digest": digest(data),
        }

    def query(self, record):
        data = sdk_call(
            "cash_query",
            {
                "huifu_id": record.snapshot["receiver_id"],
                "org_req_date": record.req_date,
                "org_req_seq_id": record.req_seq_id,
            },
            self.config,
        )
        evidence = {"response_code": data["resp_code"], "response_digest": digest(data)}
        if data["resp_code"] != "00000000":
            return {**evidence, "status": "unknown"}  # Not-found does not authorize resending.
        if (
            data.get("org_req_date") != record.req_date
            or data.get("org_req_seq_id") != record.req_seq_id
        ):
            raise DistributionUncertain()
        if money(data.get("cash_amt")) != record.amount:
            raise DistributionUncertain()
        state = data.get("trans_status")
        if data.get("re_exchange") == "Y":
            return {**evidence, "status": "attention"}  # Returned bank funds need reconciliation.
        if state not in {"S", "F", "P"} or data.get("re_exchange") not in (None, "", "N"):
            raise DistributionUncertain()
        if state == "P":
            return {**evidence, "status": "processing"}
        fee = money(data.get("fee_amt"))
        hf_seq = data.get("org_hf_seq_id", "")
        if not isinstance(hf_seq, str) or len(hf_seq) > 128:
            raise DistributionUncertain()
        return {
            **evidence,
            "status": "succeeded" if state == "S" else "failed",
            "fee_amount": fee,
            "gateway_trade_no": hf_seq,
        }
