from django.core.management.base import BaseCommand

from taskcenter.services import process_due_tasks, synchronize_provider_order_tasks


class Command(BaseCommand):
    help = "同步并处理到期的轻量任务中心任务。"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--sync-limit", type=int, default=500)

    def handle(self, *args, **options):
        synchronized = synchronize_provider_order_tasks(batch_size=options["sync_limit"])
        processed = process_due_tasks(limit=options["limit"])
        self.stdout.write(
            self.style.SUCCESS(
                "补建支付任务 %(payment_created)s 条、接单任务 %(acceptance_created)s 条；"
                "领取 %(claimed)s 条，成功 %(succeeded)s 条，取消 %(cancelled)s 条，"
                "重调度 %(rescheduled)s 条，等待重试 %(retried)s 条，失败 %(failed)s 条。"
                % {**synchronized, **processed}
            )
        )
