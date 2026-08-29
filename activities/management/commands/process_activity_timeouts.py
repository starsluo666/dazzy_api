from django.core.management.base import BaseCommand

from activities.services import process_activity_timeouts


class Command(BaseCommand):
    help = "处理活动支付超时、生命周期流转与活动结算。"

    def handle(self, *args, **options):
        result = process_activity_timeouts()
        self.stdout.write(
            self.style.SUCCESS(
                "已关闭 %(expired_payment_count)s 笔超时支付单，"
                "处理 %(processed_activity_count)s 个截止活动，"
                "开始 %(started_activity_count)s 个活动，完成 %(completed_activity_count)s 个活动，"
                "生成 %(settlement_created_count)s 笔结算，推进 %(settlement_advanced_count)s 笔结算，"
                "结算异常 %(settlement_error_count)s 笔。" % result
            )
        )
