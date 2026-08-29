from django.core.management.base import BaseCommand

from activities.services import process_activity_timeouts


class Command(BaseCommand):
    help = "关闭超时报名支付单，并处理达到成局截止时间的活动。"

    def handle(self, *args, **options):
        result = process_activity_timeouts()
        self.stdout.write(
            self.style.SUCCESS(
                "已关闭 %(expired_payment_count)s 笔超时支付单，"
                "处理 %(processed_activity_count)s 个截止活动。" % result
            )
        )
