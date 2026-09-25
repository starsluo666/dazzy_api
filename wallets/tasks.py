import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from .models import WalletRechargeOrder
from .services import confirm_recharge_payment

logger = logging.getLogger(__name__)


@shared_task(name="wallets.reconcile_pending_recharges", ignore_result=True)
def reconcile_pending_recharges():
    """Recover successful payments even if the user leaves and callbacks are lost."""
    pending = WalletRechargeOrder.objects.filter(
        status=WalletRechargeOrder.Status.PENDING_PAYMENT,
        created_at__lte=timezone.now() - timedelta(minutes=1),
    ).order_by(F("gateway_last_queried_at").asc(nulls_first=True), "created_at")
    order_numbers = list(pending.values_list("order_no", flat=True)[:100])
    for order_no in order_numbers:
        try:
            confirm_recharge_payment(order_no=order_no)
            WalletRechargeOrder.objects.filter(order_no=order_no).update(
                gateway_last_queried_at=timezone.now()
            )
        except Exception:
            # Keep a failing old payment from starving newer ones; no raw
            # gateway responses or credentials are written to logs.
            WalletRechargeOrder.objects.filter(order_no=order_no).update(
                gateway_last_queried_at=timezone.now()
            )
            logger.warning("Recharge reconciliation pending: %s", order_no)
    return len(order_numbers)
