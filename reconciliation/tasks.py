from celery import shared_task
from financeops.watchdog import monitored


@shared_task(name="reconciliation.daily_bills", ignore_result=True)
@monitored("reconciliation.daily_bills")
def daily_bills():
    from .services import daily
    return daily()
