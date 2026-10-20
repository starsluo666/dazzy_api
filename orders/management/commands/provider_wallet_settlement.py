"""Local inspection/preparation plus explicitly gated one-shot channel settlement."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.exceptions import ObjectDoesNotExist
from rest_framework.exceptions import APIException

from orders.models import ProviderOrder, ProviderOrderDistribution, ProviderOrderSettlementPlan
from orders.settlement_plans import sync_provider_settlement_plan
from orders.distribution_transport import DistributionUncertain
from orders.distributions import query_distribution
from orders.wallet_distributions import execute_wallet_distribution


class Command(BaseCommand):
    help = "余额结算：inspect 本地只读；prepare 仅准备；query 查原流水；execute 必须独立开关及显式资金确认。"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("inspect", "prepare", "execute", "query"))
        parser.add_argument("order_no")
        parser.add_argument("--confirm-real-funds", action="store_true")

    def handle(self, *args, **options):
        order_no = options["order_no"]
        if not ProviderOrder.objects.filter(order_no=order_no).exists():
            raise CommandError("订单不存在。")
        action = options["action"]
        if action == "execute" and not options["confirm_real_funds"]:
            raise CommandError("execute 会发起真实余额付款，必须显式传入 --confirm-real-funds。")
        try:
            if action == "execute":
                execute_wallet_distribution(order_no)
            elif action == "query":
                record = ProviderOrderDistribution.objects.get(settlement__order__order_no=order_no)
                if record.route != "balance":
                    raise CommandError("本单不是余额付款，请使用原分账查询命令。")
                query_distribution(order_no)
        except ObjectDoesNotExist:
            raise CommandError("尚无余额付款流水。") from None
        except (APIException, DistributionUncertain) as exc:
            raise CommandError(str(exc)) from None
        if options["action"] == "prepare":
            plan = sync_provider_settlement_plan(order_no=order_no)
        else:
            plan = ProviderOrderSettlementPlan.objects.filter(
                settlement__order__order_no=order_no,
            ).first()
        if not plan:
            raise CommandError("尚无账务结算记录，不会提前创建出款单。")
        result = {
            "order_no": order_no, "plan_no": plan.plan_no,
            "revision": plan.revision, "evaluated_at": plan.evaluated_at,
            "execution_enabled": False,
            "preparation": plan.funding_snapshot.get("wallet_settlement_preparation"),
            "notice": "仅为本地准备记录；没有发起渠道划款或增加达人收入。",
        }
        record = ProviderOrderDistribution.objects.filter(settlement=plan.settlement).first()
        if record:
            result["distribution"] = {
                "route": record.route, "status": record.status,
                "status_label": record.channel_status_label, "req_seq_id": record.req_seq_id,
                "account_payment_fee_amount": record.account_payment_fee_amount,
                "attention_reason": record.attention_reason, "last_queried_at": record.last_queried_at,
            }
            result["notice"] = "存在渠道出款记录；仅按原流水查询核验，禁止重发。银行卡到账需另查提现单。"
        self.stdout.write(json.dumps(result, ensure_ascii=False, cls=DjangoJSONEncoder))
