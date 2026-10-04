import json

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from rest_framework.exceptions import APIException

from orders.distribution_transport import DistributionUncertain
from orders.distributions import execute_distribution, query_distribution
from orders.models import ProviderOrderDistribution, ProviderOrderDistributionPreflight
from orders.distribution_preflight import preflight_summary


class Command(BaseCommand):
    help = "受控达人分账：inspect 只读本地；query 只查原流水；execute 需开关、白名单和显式确认。"

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("inspect", "execute", "query"))
        parser.add_argument("order_no")
        parser.add_argument("--confirm-real-funds", action="store_true")

    def handle(self, *args, **options):
        action, order_no = options["action"], options["order_no"]
        if action == "execute" and not options["confirm_real_funds"]:
            raise CommandError("execute 会请求真实分账，必须显式传入 --confirm-real-funds。")
        try:
            if action == "execute":
                record, _ = execute_distribution(order_no)
            elif action == "query":
                record = query_distribution(order_no)
            else:
                record = ProviderOrderDistribution.objects.filter(
                    settlement__order__order_no=order_no
                ).first()
        except ObjectDoesNotExist:
            raise CommandError("订单或分账记录不存在。") from None
        except (APIException, DistributionUncertain) as exc:
            raise CommandError(str(exc)) from None
        # No channel account IDs, keys, bank details, or signed payloads in output.
        result = {"order_no": order_no, "status": "not_started"}
        result["preflight"] = preflight_summary(
            ProviderOrderDistributionPreflight.objects.filter(
                settlement__order__order_no=order_no
            ).first()
        )
        if record:
            platform_split_amount = record.snapshot.get(
                "platform_split_amount", record.snapshot["platform_amount"]
            )
            result.update(
                req_date=record.req_date,
                req_seq_id=record.req_seq_id,
                status=record.status,
                provider_amount=record.snapshot["provider_amount"],
                platform_amount=record.snapshot["platform_amount"],
                payment_fee_flag=record.snapshot.get("fee_flag", "1"),
                platform_split_amount=platform_split_amount,
                split_amount=record.snapshot.get(
                    "split_amount", record.snapshot["provider_amount"] + platform_split_amount
                ),
                payment_fee_amount=record.payment_fee_amount,
                split_fee_amount=record.split_fee_amount,
                bank_settlement_fee_amount=None,
                bank_arrival_verified=False,
                response_code=record.response_code,
                attention_reason=record.attention_reason,
                evidence_conflict_code=record.evidence_conflict_code,
            )
        self.stdout.write(json.dumps(result, ensure_ascii=False, cls=DjangoJSONEncoder))
