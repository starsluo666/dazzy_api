from django.core.management.base import BaseCommand

from taskcenter.services import process_due_tasks, synchronize_business_tasks


class Command(BaseCommand):
    help = "同步并处理到期的轻量任务中心任务。"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--sync-limit", type=int, default=500)
        parser.add_argument(
            "--sync-only",
            action="store_true",
            help="只补建缺失任务，不领取、释放或执行任何任务。",
        )

    def handle(self, *args, **options):
        synchronized = synchronize_business_tasks(batch_size=options["sync_limit"])
        if options["sync_only"]:
            processed = {
                "released": 0,
                "claimed": 0,
                "succeeded": 0,
                "cancelled": 0,
                "rescheduled": 0,
                "retried": 0,
                "failed": 0,
            }
        else:
            processed = process_due_tasks(limit=options["limit"])
        provider = synchronized["provider_orders"]
        activities = synchronized["activities"]
        self.stdout.write(
            self.style.SUCCESS(
                "补建达人订单任务 %(provider_tasks)s 条、活动任务 %(activity_tasks)s 条；"
                "领取 %(claimed)s 条，成功 %(succeeded)s 条，取消 %(cancelled)s 条，"
                "重调度 %(rescheduled)s 条，等待重试 %(retried)s 条，失败 %(failed)s 条。"
                % {
                    "provider_tasks": sum(provider.values()),
                    "activity_tasks": sum(activities.values()),
                    **processed,
                }
            )
        )
