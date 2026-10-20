"""Default-closed financial jobs. Never retry a recorded external transfer."""

from datetime import timedelta
import logging

from celery import shared_task
from financeops.watchdog import monitored
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from orders.distributions import execute_distribution, query_distribution
from orders.distribution_preflight import record_preflight_failure
from orders.models import ProviderOrderDistribution, ProviderOrderSettlement
from orders.settlement_plans import sync_provider_settlement_plan
from .models import ProviderWithdrawal
from .receiving_onboarding import refresh_onboarding
from .withdrawals import query_withdrawal

logger = logging.getLogger(__name__)


@shared_task(name="providers.process_income_transfers", ignore_result=True)
@monitored("providers.process_income_transfers")
def process_income_transfers():
    if not settings.HUIFU_PROVIDER_INCOME_JOBS_ENABLED:
        return {"disabled": True}
    counts = {
        "distribution_submitted": 0,
        "balance_payment_submitted": 0,
        "distribution_queried": 0,
        "withdrawal_queried": 0,
        "deferred": 0,
    }
    # Local settlement freeze/after-sale rules are handled by taskcenter first.
    # Rotate candidates by plan.evaluated_at so an ineligible order cannot starve others.
    if settings.HUIFU_PROVIDER_DISTRIBUTION_ENABLED:
        candidates = (
            ProviderOrderSettlement.objects.filter(
                status="settled",
                freeze_until__lte=timezone.now(),
                distribution__isnull=True,
                order__payment_order__delay_acct_flag="Y",
                order__fulfillment_review_required=False,
                distribution_plan__requires_manual_review=False,
            )
            .exclude(distribution_preflight__status="blocked")
            .filter(
                Q(distribution_preflight__next_retry_at__isnull=True)
                | Q(distribution_preflight__next_retry_at__lte=timezone.now())
            )
            .select_related("provider__user", "order")
            .order_by("distribution_plan__evaluated_at", "pk")[:20]
        )
        for settlement in candidates:
            try:
                try:
                    refresh_onboarding(settlement.provider)
                except Exception:
                    record_preflight_failure(settlement.order.order_no, "receiving_account")
                    raise
                _, created = execute_distribution(settlement.order.order_no)
                counts["distribution_submitted"] += int(created)
            except Exception:
                counts["deferred"] += 1
                # No raw channel exceptions, names, card data or credentials in logs.
                logger.warning(
                    "Provider income distribution deferred: %s", settlement.order.order_no
                )
            finally:
                # Re-evaluate locally so rotation keeps a truthful plan/audit snapshot.
                try:
                    sync_provider_settlement_plan(order_no=settlement.order.order_no)
                except Exception:
                    logger.warning(
                        "Provider settlement plan refresh deferred: %s", settlement.order.order_no
                    )
    if settings.HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED:
        from orders.wallet_distributions import execute_wallet_distribution, rollout_start

        start = rollout_start()
        # An explicit rollout time is required; never scan/backpay old orders.
        candidates = [] if start is None else (
            ProviderOrderSettlement.objects.filter(
                status="settled", freeze_until__lte=timezone.now(), distribution__isnull=True,
                provider_settlement_amount__gt=0,
                order__paid_at__gte=start, order__fulfillment_review_required=False,
                distribution_plan__requires_manual_review=False,
                distribution_plan__funding_type__in=("wallet", "mixed"),
            )
            .exclude(distribution_preflight__status="blocked")
            .filter(Q(distribution_preflight__next_retry_at__isnull=True)
                    | Q(distribution_preflight__next_retry_at__lte=timezone.now()))
            .select_related("provider__user", "order")
            .order_by("distribution_plan__evaluated_at", "pk")[:20]
        )
        for settlement in candidates:
            try:
                try:
                    refresh_onboarding(settlement.provider)
                except Exception:
                    record_preflight_failure(settlement.order.order_no, "receiving_account")
                    raise
                _, created = execute_wallet_distribution(settlement.order.order_no)
                counts["balance_payment_submitted"] += int(created)
            except Exception:
                counts["deferred"] += 1
                logger.warning("Provider balance settlement deferred: %s", settlement.order.order_no)
            finally:
                try:
                    sync_provider_settlement_plan(order_no=settlement.order.order_no)
                except Exception:
                    logger.warning("Provider balance plan refresh deferred: %s", settlement.order.order_no)
    pending = (
        ProviderOrderDistribution.objects.filter(status__in=("submitting", "processing", "unknown"))
        .select_related("settlement__order")
        .order_by(F("last_queried_at").asc(nulls_first=True), "pk")[:50]
    )
    # Separate quota prevents successful-record reconciliation from starving
    # pending requests. Recheck all successes over time, not just a 7-day window.
    due_successes = (
        ProviderOrderDistribution.objects.filter(status="succeeded")
        .filter(Q(last_queried_at__isnull=True)
                | Q(last_queried_at__lte=timezone.now() - timedelta(hours=6)))
        .select_related("settlement__order")
        .order_by(F("last_queried_at").asc(nulls_first=True), "pk")[:10]
    )
    # Materialize both lists before querying so a newly confirmed success is not
    # selected again in this run, even if the scheduler's clock changes.
    for record in [*pending, *due_successes]:
        try:
            query_distribution(record.settlement.order.order_no)
            counts["distribution_queried"] += 1
        except Exception:
            counts["deferred"] += 1
            ProviderOrderDistribution.objects.filter(pk=record.pk).update(
                last_queried_at=timezone.now()
            )
    # Recheck recent successes too: bank return is distinct from initial success.
    withdrawals = ProviderWithdrawal.objects.filter(
        Q(status__in=("submitting", "processing", "unknown"))
        | Q(status="succeeded", created_at__gte=timezone.now() - timedelta(days=7))
    ).order_by(F("last_queried_at").asc(nulls_first=True), "pk")[:50]
    for record in withdrawals:
        try:
            query_withdrawal(record)
            counts["withdrawal_queried"] += 1
        except Exception:
            counts["deferred"] += 1
            ProviderWithdrawal.objects.filter(pk=record.pk).update(last_queried_at=timezone.now())
    return counts
