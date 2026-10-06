import logging

from celery import shared_task

from .coupon_batches import process_coupon_batch
from .models import CouponIssueBatch

logger = logging.getLogger(__name__)


@shared_task(name="orders.process_coupon_issue_batches", ignore_result=True)
def process_coupon_issue_batches():
    # Poll persisted work: broker delivery failures cannot lose a confirmed batch.
    batch_ids = CouponIssueBatch.objects.filter(status__in=("queued", "running")).order_by("id").values_list("id", flat=True)[:10]
    for batch_id in list(batch_ids):
        try:
            process_coupon_batch(batch_id)
        except Exception:
            logger.exception("Coupon batch chunk rolled back: batch=%s", batch_id)
