"""Sanitized execution diagnostics; never store raw exceptions or channel payloads."""

from datetime import timedelta
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import ProviderOrder, ProviderOrderDistribution, ProviderOrderDistributionPreflight

logger = logging.getLogger(__name__)
REASONS = {
    "configuration": "分账开关、白名单、额度或渠道配置未满足，请检查配置。",
    "local_conditions": "订单、冻结期、退款或资金来源条件未满足，请查看业务准备记录。",
    "receiving_account": "达人收款账户、提现方式或费用承担规则尚未核实。",
    "payment_proof": "原支付或渠道费用明细暂未核实，尚未发起分账。",
    "payment_identity": "原支付交易标识或延时状态无法核实，请人工核账。",
    "payment_amount": "原支付金额与本地记录不一致，请人工核账。",
    "payment_fee_policy": "支付扣费方式或费用承担方与原支付快照不一致。",
    "platform_fee_shortfall": "平台份额不足以承担支付手续费，禁止扣减达人收入。",
    "available_amount": "渠道可分账金额与本次分账总额不一致，请人工核账。",
    "snapshot_changed": "核验期间订单或收款资料发生变化，尚未发起分账。",
    "refunded_order": "订单已有成功退款，当前试点不支持退款后的分账，请人工核账。",
}


class DistributionBlocked(ValidationError):
    def __init__(self, code):
        self.code = code
        super().__init__(REASONS[code])


@transaction.atomic
def record_preflight_failure(order_no, code):
    code = code if code in REASONS else "payment_proof"
    order = ProviderOrder.objects.select_for_update().get(order_no=order_no)
    # A concurrent successful registration always wins over a late failure.
    if ProviderOrderDistribution.objects.filter(settlement__order=order).exists():
        return
    settlement = getattr(order, "settlement", None)
    if not settlement:
        return
    now = timezone.now()
    record, _ = ProviderOrderDistributionPreflight.objects.get_or_create(
        settlement=settlement, defaults={"checked_at": now, "status": "deferred"},
    )
    if record.reason_code != code:
        record.failure_count = 0
    record.failure_count += 1
    record.status = "blocked" if code == "refunded_order" else "deferred"
    record.reason_code, record.reason_message = code, REASONS[code]
    record.checked_at = now
    record.next_retry_at = (
        None if record.status == "blocked" else
        now + timedelta(minutes=min(60, 2 ** min(record.failure_count - 1, 6)))
    )
    record.save()
    # First failure is visible; persistent failures escalate without log flooding.
    count = record.failure_count
    if count == 1 or count == 3 or (count >= 4 and count & (count - 1) == 0):
        level = logging.ERROR if count >= 3 or record.status == "blocked" else logging.WARNING
        logger.log(level, "Provider split preflight: order=%s reason=%s failures=%s", order_no, code, count)


def mark_preflight_registered(settlement_id):
    # Called inside the same order-locked transaction as durable registration.
    ProviderOrderDistributionPreflight.objects.update_or_create(
        settlement_id=settlement_id,
        defaults={"status": "registered", "reason_code": "", "reason_message": "",
                  "checked_at": timezone.now(), "next_retry_at": None},
    )


def preflight_summary(record):
    if record is None:
        return None
    return {
        "status": record.status, "status_label": record.get_status_display(),
        "reason_code": record.reason_code, "reason_message": record.reason_message,
        "failure_count": record.failure_count, "checked_at": record.checked_at,
        "next_retry_at": record.next_retry_at,
    }
