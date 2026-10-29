from celery import shared_task
from django.conf import settings
import logging

from .services import pending_ids, process_one


@shared_task(name="moderation.process_pending", ignore_result=True)
def process_pending():
    if not settings.CONTENT_REVIEW_ENABLED or not settings.TENCENT_CONTENT_REVIEW_ENABLED:
        return {"disabled": True}
    processed = 0
    for pk in pending_ids():
        try:
            processed += bool(process_one(pk))
        except Exception:
            # No response payload, user text or credentials in worker logs.
            logging.getLogger(__name__).warning(
                "Content review %s deferred after processing failure", pk
            )
    return processed
