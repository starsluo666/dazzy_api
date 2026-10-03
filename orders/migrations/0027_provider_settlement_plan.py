import django.db.models.deletion
from django.db import migrations, models
import orders.models


class Migration(migrations.Migration):
    dependencies = [("orders", "0026_payment_wechat_payer_digest")]

    # Schema only: no historical backfill, SDK calls, wallet or payment mutation.
    operations = [
        migrations.CreateModel(
            name="ProviderOrderSettlementPlan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "plan_no",
                    models.CharField(
                        default=orders.models.generate_provider_settlement_plan_no,
                        editable=False,
                        max_length=24,
                        unique=True,
                        verbose_name="分账准备单号",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("waiting", "等待结算条件"),
                            ("blocked", "待接通或核实资金链路"),
                            ("cancelled", "无需分账"),
                        ],
                        default="blocked",
                        max_length=24,
                    ),
                ),
                (
                    "funding_type",
                    models.CharField(
                        choices=[
                            ("unknown", "资金来源待核实"),
                            ("external", "全额外部支付"),
                            ("wallet", "余额支付"),
                            ("mixed", "混合支付"),
                        ],
                        default="unknown",
                        max_length=16,
                    ),
                ),
                ("paid_amount", models.PositiveBigIntegerField(verbose_name="订单实付（分）")),
                ("refunded_amount", models.PositiveBigIntegerField(verbose_name="已退款（分）")),
                (
                    "provider_amount",
                    models.PositiveBigIntegerField(verbose_name="达人应结算（分）"),
                ),
                ("platform_amount", models.PositiveBigIntegerField(verbose_name="平台抽成（分）")),
                (
                    "funding_snapshot",
                    models.JSONField(default=dict, verbose_name="支付来源核对快照"),
                ),
                ("blockers", models.JSONField(default=list, verbose_name="未出款原因")),
                (
                    "requires_manual_review",
                    models.BooleanField(default=True, verbose_name="历史结算需人工核账"),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                ("evaluated_at", models.DateTimeField(verbose_name="最近核对时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "settlement",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="distribution_plan",
                        to="orders.providerordersettlement",
                    ),
                ),
            ],
            options={
                "db_table": "provider_order_settlement_plan",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            paid_amount=models.F("refunded_amount")
                            + models.F("provider_amount")
                            + models.F("platform_amount")
                        ),
                        name="provider_plan_amount_matches",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ProviderOrderSettlementPlanRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("snapshot", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="revisions",
                        to="orders.providerordersettlementplan",
                    ),
                ),
            ],
            options={
                "db_table": "provider_order_settlement_plan_revision",
                "ordering": ("revision",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("plan", "revision"), name="uniq_provider_plan_revision"
                    )
                ],
            },
        ),
    ]
