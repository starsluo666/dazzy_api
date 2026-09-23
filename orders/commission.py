"""Provider revenue bonuses are snapshotted when an order is created.

Turnover means completed-order net service fees (after coupon and successful
refunds), not transport fees or the provider's share after commission. An order
counts when confirmed complete, even while its settlement is in the normal
risk-freeze period; disputed/cancelled settlements do not count.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db.models import Sum
from django.utils import timezone


PERIODS = ("month", "quarter", "year", "never")


def validate_commission_tiers(tiers):
    if not isinstance(tiers, list) or len(tiers) > 5:
        raise ValueError("阶梯最多配置 5 级。")
    previous_threshold = -1
    previous_bonus = Decimal("-1")
    normalized = []
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict) or set(tier) != {"threshold_amount", "bonus_rate"}:
            raise ValueError("每级必须填写营业额起点（分）和加成比例（%）。")
        threshold = tier["threshold_amount"]
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            raise ValueError("营业额起点必须是非负整数分。")
        try:
            bonus = Decimal(str(tier["bonus_rate"]))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("加成比例格式不正确。") from exc
        if not bonus.is_finite() or bonus < 0 or bonus > 100 or bonus.as_tuple().exponent < -2:
            raise ValueError("加成比例必须在 0–100% 之间，最多两位小数。")
        if threshold <= previous_threshold or bonus < previous_bonus:
            raise ValueError("营业额起点必须递增，加成比例不能下降。")
        if index == 0 and threshold != 0:
            raise ValueError("首级营业额起点必须为 0。")
        normalized.append({"threshold_amount": threshold, "bonus_rate": str(bonus)})
        previous_threshold, previous_bonus = threshold, bonus
    return normalized


def turnover_period_start(period, now):
    if period == "never":
        return None
    local = timezone.localtime(now)
    month = local.month
    if period == "quarter":
        month = ((month - 1) // 3) * 3 + 1
    elif period == "year":
        month = 1
    return timezone.make_aware(datetime(local.year, month, 1), local.tzinfo)


def commission_snapshot(provider, category_rate, *, now=None):
    from backoffice.operation_settings import platform_operation_rules
    from .models import ProviderOrderSettlement

    now = now or timezone.now()
    rules = platform_operation_rules()
    period = provider.commission_reset_period_override or rules["provider_commission_reset_period"]
    tiers = (provider.commission_tiers_override if provider.commission_tiers_override is not None
             else rules["provider_commission_tiers"])
    period_start = turnover_period_start(period, now)
    queryset = ProviderOrderSettlement.objects.filter(
        provider=provider,
        status__in=(
            ProviderOrderSettlement.Status.RISK_FROZEN,
            ProviderOrderSettlement.Status.SETTLED,
        ),
    )
    if period_start is not None:
        queryset = queryset.filter(frozen_at__gte=period_start)
    turnover = queryset.aggregate(total=Sum("net_service_fee_amount"))["total"] or 0
    bonus = Decimal("0")
    for tier in tiers:
        if turnover >= tier["threshold_amount"]:
            bonus = Decimal(str(tier["bonus_rate"]))
    category_rate = Decimal(str(category_rate))
    applied_bonus = min(category_rate, bonus)
    return {
        "platform_commission_rate": str(category_rate - applied_bonus),
        "category_platform_commission_rate": str(category_rate),
        "provider_bonus_rate": str(applied_bonus),
        "provider_bonus_configured_rate": str(bonus),
        "provider_bonus_turnover_amount": turnover,
        "provider_bonus_period": period,
        "provider_bonus_period_start": period_start.isoformat() if period_start else None,
        "provider_bonus_snapshot_at": now.isoformat(),
    }
