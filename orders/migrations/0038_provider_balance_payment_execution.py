import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0037_usercoupon_name_snapshot_couponcampaign_and_more")]

    operations = [
        migrations.CreateModel(
            name="ProviderBalancePaymentPool",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("scope", models.CharField(max_length=64, unique=True)),
                ("generation", models.PositiveBigIntegerField(default=0)),
                ("hold_reason", models.CharField(blank=True, max_length=200)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "provider_balance_payment_pool"},
        ),
        migrations.AddField(
            model_name="providerorderdistribution", name="route",
            field=models.CharField(choices=[("delayed", "原支付延时分账"), ("balance", "平台余额支付结算")], default="delayed", max_length=16),
        ),
        migrations.AlterField(
            model_name="providerorderdistribution", name="payment_fee_amount",
            field=models.PositiveBigIntegerField(null=True, verbose_name="支付手续费（分）"),
        ),
        migrations.AddField(
            model_name="providerorderdistribution", name="account_payment_fee_amount",
            field=models.PositiveBigIntegerField(null=True, verbose_name="余额支付手续费（分）"),
        ),
        migrations.AddField(
            model_name="providerorderdistribution", name="funding_pool",
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="orders.providerbalancepaymentpool"),
        ),
        migrations.AddField(
            model_name="providerorderdistribution", name="funds_reserved_amount",
            field=models.PositiveBigIntegerField(null=True, verbose_name="本次付款及费用预留（分）"),
        ),
        migrations.AddField(
            model_name="providerorderdistribution", name="funds_released",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="providerorderdistribution",
            constraint=models.UniqueConstraint(
                fields=("funding_pool",),
                condition=models.Q(funding_pool__isnull=False, funds_released=False),
                name="one_pending_provider_balance_payment_per_pool",
            ),
        ),
    ]
