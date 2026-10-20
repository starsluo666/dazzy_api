from celery import shared_task


@shared_task(name="reconciliation.daily_bills", ignore_result=True)
def daily_bills():
    from .services import daily
    return daily()
