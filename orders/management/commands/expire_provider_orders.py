from django.core.management.base import BaseCommand

from orders.services import expire_pending_orders


class Command(BaseCommand):
    help = "关闭支付超时的达人订单并释放档期"

    def handle(self, *args, **options):
        count = expire_pending_orders()
        self.stdout.write(self.style.SUCCESS(f"已关闭 {count} 个支付超时订单。"))
