import json

from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import APIException

from providers.models import ProviderWithdrawal
from providers.withdrawals import query_withdrawal, withdrawal_data


class Command(BaseCommand):
    help = "达人提现 inspect 只读本地，query 只查原渠道流水；不支持替达人创建或重发提现。"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("inspect", "query"))
        parser.add_argument("withdrawal_no")

    def handle(self, *args, **options):
        record = ProviderWithdrawal.objects.filter(req_seq_id=options["withdrawal_no"]).first()
        if not record:
            raise CommandError("提现记录不存在。")
        if options["action"] == "query":
            try:
                record = query_withdrawal(record)
            except APIException:
                raise CommandError("提现查询暂未完成，请核对渠道配置或稍后查询原流水。") from None
        self.stdout.write(json.dumps(withdrawal_data(record), ensure_ascii=False, default=str))
