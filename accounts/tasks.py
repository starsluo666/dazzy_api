import logging
from collections import Counter

from celery import shared_task
from django.db.models import F
from django.utils import timezone

from .account_closure import process_account_closure
from .models import AccountClosureRequest

logger = logging.getLogger(__name__)


@shared_task(name="accounts.process_due_account_closures", ignore_result=True)
def process_due_account_closures():
    # Old blocked/error requests must not starve later due requests.
    user_ids = list(AccountClosureRequest.objects.filter(
        status__in=("pending", "blocked"), execute_after__lte=timezone.now(),
    ).order_by(F("checked_at").asc(nulls_first=True), "execute_after").values_list("user_id", flat=True)[:200])
    counts = Counter()
    for user_id in user_ids:
        try:
            counts[process_account_closure(user_id)] += 1
        except Exception:
            AccountClosureRequest.objects.filter(
                user_id=user_id, status__in=("pending", "blocked"),
            ).update(checked_at=timezone.now())
            logger.exception("Account closure processing failed for user ID %s", user_id)
            counts["failed"] += 1
    return dict(counts)
