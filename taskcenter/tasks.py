from celery import shared_task

from .services import process_due_tasks, synchronize_business_tasks


@shared_task(name="taskcenter.process_due_scheduled_tasks", ignore_result=True)
def process_due_scheduled_tasks():
    return process_due_tasks(limit=100)


@shared_task(name="taskcenter.synchronize_scheduled_tasks", ignore_result=True)
def synchronize_scheduled_tasks():
    return synchronize_business_tasks()
