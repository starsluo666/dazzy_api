from django.core.management.base import BaseCommand

from taskcenter.models import ScheduledTask
from taskcenter.services import process_due_tasks, synchronize_provider_order_tasks


class Command(BaseCommand):
    help = "关闭支付超时的达人订单并释放档期"

    def handle(self, *args, **options):
        synchronized = synchronize_provider_order_tasks()
        result = process_due_tasks(
            limit=100,
            task_types=(ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,),
        )
        self.stdout.write(
            self.style.SUCCESS(
                "补建 %(payment_created)s 条支付超时任务，已关闭 %(succeeded)s 个订单，"
                "取消 %(cancelled)s 条无效任务，失败 %(failed)s 条。"
                % {**synchronized, **result}
            )
        )
