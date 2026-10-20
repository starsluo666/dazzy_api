import json

from django.core.management.base import BaseCommand, CommandError

from financeops.watchdog import health


class Command(BaseCommand):
    help = "只读巡检支付任务；可由独立于 Celery 的服务器监控定时执行。异常时非零退出。"

    def handle(self, **options):
        rows = health()
        self.stdout.write(json.dumps(rows, ensure_ascii=False, default=str))
        if any(row["alert"] for row in rows):
            raise CommandError("支付任务巡检发现异常；未执行重试或资金操作。")
