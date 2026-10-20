from datetime import date

from django.core.management.base import BaseCommand, CommandError

from reconciliation.models import ReconciliationBatch
from reconciliation.services import daily, ensure_day, run_batch
from orders.huifu import HuifuPaymentConfig


class Command(BaseCommand):
    help = "只核对渠道账单并登记差异；不发起付款、退款或修改业务账。"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("inspect", "run"))
        parser.add_argument("--date", type=date.fromisoformat)

    def handle(self, *args, **options):
        if options["action"] == "inspect":
            qs = ReconciliationBatch.objects.all()
            if options["date"]:
                qs = qs.filter(data_date=options["date"])
            for batch in qs.order_by("-data_date", "bill_type")[:50]:
                self.stdout.write(f"{batch.data_date} {batch.get_bill_type_display()} {batch.get_status_display()} {batch.error_code}")
            return
        from django.conf import settings
        if not settings.HUIFU_RECONCILIATION_ENABLED:
            raise CommandError("每日对账尚未启用；inspect 可只读查看本地结果。")
        if not options["date"]:
            self.stdout.write(str(daily()))
            return
        from reconciliation.services import start_date
        from django.utils import timezone
        if not start_date() or not start_date() <= options["date"] < timezone.localdate():
            raise CommandError("日期须在配置的启用日期之后且早于今天。")
        config = HuifuPaymentConfig.from_settings()
        for batch in ensure_day(options["date"], config):
            run_batch(batch.pk, config=config)
        self.stdout.write("本地对账记录已更新；未执行资金操作。")
