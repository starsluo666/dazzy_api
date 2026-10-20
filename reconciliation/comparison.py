"""Strict template adapter. Unknown formats produce work, never a false match."""
from collections import defaultdict
from datetime import datetime
import re

from orders.huifu import _canonical_digest
from .gateway import BillError, money

HEADERS = {
    "TRADE_BILL": {"商户号", "汇付订单号", "请求流水号", "请求日期", "交易类型", "交易金额", "交易状态", "商户手续费"},
    "SPLIT_BILL": {"商户号", "请求流水号", "请求日期", "入账方商户号", "分账金额", "账务状态", "分账手续费"},
    "WITHDRAWAL_BILL": {"商户号", "请求流水号", "请求日期", "交易金额", "交易状态", "交易手续费", "汇付订单号"},
    "SETTLE_BILL": {"商户号", "结算日期", "结算金额", "结算交易状态", "手续费"},
    "SETTLE_USER_BILL": {"商户号", "结算日期", "结算金额", "结算交易状态", "手续费"},
}
REFUNDS = {"退货", "交易退款", "网银退款", "直连支付宝退款", "直连微信退款", "花呗分期交易退款", "银行卡分期交易退款"}
PAYMENTS = {"消费", "B2B网银支付", "B2C网银支付", "B2B网银充值", "B2C网银充值", "快捷支付", "快捷充值", "手机网页支付", "直连微信统一下单"}


def normalize(raw, bill_type, merchant):
    result = []
    for row in raw:
        if "__header__" in row:
            if not HEADERS[bill_type].issubset(row["__header__"]):
                raise BillError("unsupported_schema")
            continue
        # Unrelated/unknown merchant lines are not silently accepted or discarded.
        if row.get("商户号", "").strip() != merchant:
            raise BillError("bill_merchant_mismatch")
        if bill_type.startswith("SETTLE"):
            amount, fee = money(row["结算金额"]), money(row["手续费"]) if row["手续费"].strip() else None
            result.append({"key": ("", ""), "kind": "settlement", "amount": amount, "fee": fee,
                           "status": row["结算交易状态"].strip(), "trade": "", "receiver": "", "original": ""})
            continue
        date, seq = row["请求日期"].strip(), row["请求流水号"].strip()
        if not re.fullmatch(r"\d{8}", date) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", seq):
            raise BillError("row_identity")
        try:
            datetime.strptime(date, "%Y%m%d")
        except ValueError:
            raise BillError("row_identity") from None
        split = bill_type == "SPLIT_BILL"
        kind = "split" if split else "withdrawal"
        if bill_type == "TRADE_BILL":
            typ = row["交易类型"].strip()
            kind = "balance" if typ == "余额支付" else "refund" if typ in REFUNDS else "payment" if typ in PAYMENTS else "unsupported"
        fee_field = "分账手续费" if split else "交易手续费" if bill_type == "WITHDRAWAL_BILL" else "商户手续费"
        result.append({
            "key": (date, seq), "kind": kind, "amount": money(row["分账金额" if split else "交易金额"]),
            "fee": money(row[fee_field]) if row[fee_field].strip() else None,
            "account_payment_fee": money(row["交易手续费"]) if split and row.get("交易手续费", "").strip() else None,
            "status": row["账务状态" if split else "交易状态"].strip(),
            "trade": row.get("汇付订单号", "").strip(),
            "receiver": row.get("入账方商户号", "").strip(),
            "original": row.get("原交易汇付订单号", row.get("原汇付交易订单号", "")).strip(),
        })
    return result


def finding(category, reference="", **evidence):
    messages = {
        "missing_local": "渠道账单有记录，本地未找到唯一对应流水。",
        "missing_channel": "本地成功记录未在本日账单中找到，请核实跨日入账或账单范围。",
        "mismatch": "金额、状态、原交易或收款明细不一致，请按原流水核查。",
        "duplicate": "存在重复或不唯一的交易记录，不能自动认定一致。",
        "unsupported": "账单记录类型或状态不在已核验范围，请人工核查。",
        "fee_unknown": "部分手续费缺少可比较的本地或渠道证据，费用尚未核对完整。",
        "settlement_manual": "结算账单没有逐笔请求流水号，须财务核对银行回单与提现记录。",
        "withdrawal_coverage": "出金模板不保证覆盖个人手动提现，须结合用户结算账单及原提现查询核查。",
    }
    return {"category": category, "reference": reference, "message": messages[category], "evidence": evidence}


def compare(channel, local, bill_type):
    issues, groups, expected = [], defaultdict(list), defaultdict(list)
    for row in channel:
        groups[row["key"]].append(row)
    for row in local:
        expected[row["key"]].append(row)
    matched, fee_unknown = 0, 0
    if bill_type.startswith("SETTLE"):
        if channel:
            issues.append(finding("settlement_manual", count=len(channel),
                                  channel_amount=sum(row["amount"] for row in channel)))
        return issues, {"channel_count": len(channel), "matched_count": 0, "coverage": "manual"}
    if bill_type == "WITHDRAWAL_BILL" and (local or channel):
        issues.append(finding("withdrawal_coverage", local_count=len(local)))
    for key, rows in groups.items():
        candidates = expected.get(key, [])
        reference = key[1]
        if not candidates:
            issues.append(finding("missing_local", reference, request_date=key[0], channel_amount=sum(r["amount"] for r in rows)))
            continue
        if len(candidates) != 1 or (bill_type != "SPLIT_BILL" and len(rows) != 1):
            issues.append(finding("duplicate", reference, request_date=key[0]))
            continue
        item = candidates[0]
        amount = sum(r["amount"] for r in rows)
        fields = []
        if amount != item["amount"]:
            fields.append("金额")
        if any(r["status"] not in ("成功", "失败") or r["kind"] == "unsupported" for r in rows):
            issues.append(finding("unsupported", reference, request_date=key[0]))
            continue
        if any(r["status"] != item["status"] or r["kind"] != item["kind"] for r in rows):
            fields.append("状态或类型")
        if bill_type == "SPLIT_BILL":
            recipients = {r["receiver"]: r["amount"] for r in rows}
            if len(recipients) != len(rows) or recipients != item["recipients"]:
                fields.append("收款明细")
        elif not item["trade"] or any(r["trade"] != item["trade"] for r in rows):
            fields.append("渠道流水")
        if item["original"] and any(r["original"] != item["original"] for r in rows):
            fields.append("原交易")
        # A split fee is not necessarily repeated/allocated per receiver row.
        # Without a verified per-line fee rule, do not sum it into a request fee.
        fee_field = "account_payment_fee" if item.get("route") == "balance" and bill_type == "SPLIT_BILL" else "fee"
        fee = rows[0].get(fee_field) if len(rows) == 1 else None
        if fee is None or item["fee"] is None:
            fee_unknown += 1
        elif fee != item["fee"]:
            fields.append("手续费")
        if fields:
            issues.append(finding("mismatch", item["reference"], request_date=key[0], request_seq=reference,
                                  fields=fields, local_amount=item["amount"], channel_amount=amount,
                                  local_fee=item["fee"], channel_fee=fee))
        else:
            matched += 1
    for key, candidates in expected.items():
        if key not in groups:
            for item in candidates:
                if item["expected"]:
                    issues.append(finding("missing_channel", item["reference"], request_date=key[0],
                                          request_seq=key[1], local_amount=item["amount"]))
    if fee_unknown:
        issues.append(finding("fee_unknown", count=fee_unknown))
    return issues, {"channel_count": len(channel), "local_count": len(local), "matched_count": matched,
                    "fee_unverified_count": fee_unknown, "coverage": "rows_and_available_fees"}


def issue_identity(item):
    evidence = item["evidence"]
    # Stable across re-imports and changes of amount/status, not row numbers.
    return _canonical_digest([item["category"], evidence.get("request_seq", item["reference"]),
                              evidence.get("request_date", "")])
