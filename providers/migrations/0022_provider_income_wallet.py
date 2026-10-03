import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0021_alter_providerreceivingaccount_options_and_more"),
        ("orders", "0029_provider_order_distribution"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerreceivingaccount",
            name="cash_status",
            field=models.CharField(blank=True, max_length=1),
        ),
        migrations.AddField(
            model_name="providerreceivingaccount",
            name="automatic_settlement_disabled",
            field=models.BooleanField(default=None, null=True),
        ),
        migrations.AddField(
            model_name="providerreceivingaccount",
            name="verified_cash_config",
            field=models.JSONField(default=dict, editable=False),
        ),
        migrations.AddField(
            model_name="providerreceivingaccount",
            name="cash_card_ciphertext",
            field=models.TextField(blank=True, editable=False),
        ),
        migrations.CreateModel(
            name="ProviderIncomeWallet",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("available_amount", models.PositiveBigIntegerField(default=0)),
                ("reserved_amount", models.PositiveBigIntegerField(default=0)),
                ("paid_amount", models.PositiveBigIntegerField(default=0)),
                ("channel_scope", models.CharField(max_length=64)),
                ("receiver_id", models.CharField(editable=False, max_length=18)),
                ("hold_reason", models.CharField(blank=True, max_length=200)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "provider",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="income_wallet",
                        to="providers.providerprofile",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProviderWithdrawal",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("request_key", models.UUIDField()),
                ("req_seq_id", models.CharField(max_length=32, unique=True)),
                ("req_date", models.CharField(max_length=8)),
                ("amount", models.PositiveBigIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("submitting", "提交中"),
                            ("processing", "银行处理中"),
                            ("unknown", "结果核实中"),
                            ("succeeded", "提现成功"),
                            ("failed", "提现失败，金额已退回余额"),
                            ("attention", "需人工核账"),
                        ],
                        default="submitting",
                        max_length=16,
                    ),
                ),
                ("snapshot", models.JSONField(default=dict, editable=False)),
                ("fee_amount", models.PositiveBigIntegerField(null=True)),
                ("response_code", models.CharField(blank=True, max_length=8)),
                ("response_digest", models.CharField(blank=True, max_length=64)),
                ("gateway_trade_no", models.CharField(blank=True, max_length=128)),
                ("last_queried_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="withdrawals",
                        to="providers.providerincomewallet",
                    ),
                ),
            ],
            options={
                "ordering": ("-pk",),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("wallet", "request_key"), name="provider_withdrawal_idempotency"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="ProviderIncomeEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("source_key", models.CharField(max_length=80, unique=True)),
                ("kind", models.CharField(max_length=16)),
                ("amount", models.PositiveBigIntegerField()),
                ("available_delta", models.BigIntegerField()),
                ("reserved_delta", models.BigIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "distribution",
                    models.OneToOneField(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="orders.providerorderdistribution",
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="entries",
                        to="providers.providerincomewallet",
                    ),
                ),
                (
                    "withdrawal",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="providers.providerwithdrawal",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProviderWithdrawalObservation",
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
                    "withdrawal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="observations",
                        to="providers.providerwithdrawal",
                    ),
                ),
            ],
        ),
    ]
