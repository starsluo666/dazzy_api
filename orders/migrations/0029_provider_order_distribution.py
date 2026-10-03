import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0028_provider_settlement_fee_policy")]
    # No cohort backfill: all existing payment orders remain non-delayed.
    operations = [
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="delay_acct_flag",
            field=models.CharField(default="N", max_length=1, verbose_name="延时交易标记"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="distribution_cohort",
            field=models.JSONField(default=dict, verbose_name="首次下单分账范围快照"),
        ),
        migrations.CreateModel(
            name="ProviderOrderDistribution",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("req_date", models.CharField(max_length=8)),
                ("req_seq_id", models.CharField(max_length=32, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("submitting", "分账请求已登记"),
                            ("unknown", "分账结果待核实"),
                            ("processing", "渠道分账处理中"),
                            ("succeeded", "渠道分账成功（非银行卡到账）"),
                            ("failed", "渠道分账失败（禁止自动重发）"),
                        ],
                        default="submitting",
                        max_length=16,
                    ),
                ),
                ("snapshot", models.JSONField(verbose_name="不可变分账请求快照")),
                (
                    "payment_fee_amount",
                    models.PositiveBigIntegerField(verbose_name="支付手续费（分）"),
                ),
                (
                    "split_fee_amount",
                    models.PositiveBigIntegerField(null=True, verbose_name="分账手续费（分）"),
                ),
                ("gateway_trade_no", models.CharField(blank=True, max_length=128)),
                ("response_code", models.CharField(blank=True, max_length=8)),
                ("response_digest", models.CharField(blank=True, max_length=64)),
                ("attention_reason", models.CharField(blank=True, max_length=200)),
                ("last_queried_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "settlement",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="distribution",
                        to="orders.providerordersettlement",
                    ),
                ),
            ],
            options={"db_table": "provider_order_distribution"},
        ),
        migrations.CreateModel(
            name="ProviderOrderDistributionObservation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("kind", models.CharField(max_length=8)),
                ("status", models.CharField(max_length=16)),
                ("response_code", models.CharField(blank=True, max_length=8)),
                ("response_digest", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "distribution",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="observations",
                        to="orders.providerorderdistribution",
                    ),
                ),
            ],
            options={"db_table": "provider_distribution_observation"},
        ),
    ]
