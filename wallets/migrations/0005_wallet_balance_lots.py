import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("wallets", "0004_recharge_wechat_payer_digest")]

    operations = [
        migrations.CreateModel(
            name="WalletBalanceLot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("discount_rate_bps", models.PositiveSmallIntegerField(default=10000)),
                ("credited_amount", models.PositiveBigIntegerField()),
                ("available_amount", models.PositiveBigIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("wallet", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="balance_lots", to="wallets.userwallet")),
                ("recharge_order", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="balance_lot", to="wallets.walletrechargeorder")),
            ],
            options={
                "db_table": "wallet_balance_lot", "ordering": ("discount_rate_bps", "id"),
                "constraints": [
                    models.CheckConstraint(condition=models.Q(discount_rate_bps__gte=1, discount_rate_bps__lte=10000), name="wallet_lot_rate_valid"),
                    models.CheckConstraint(condition=models.Q(available_amount__lte=models.F("credited_amount")), name="wallet_lot_available_valid"),
                ],
            },
        ),
        migrations.CreateModel(
            name="WalletLotDebit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.PositiveBigIntegerField()),
                ("restored_amount", models.PositiveBigIntegerField(default=0)),
                ("allocation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lot_debits", to="wallets.walletpaymentallocation")),
                ("lot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="debits", to="wallets.walletbalancelot")),
            ],
            options={
                "db_table": "wallet_lot_debit", "ordering": ("id",),
                "constraints": [
                    models.UniqueConstraint(fields=("allocation", "lot"), name="uniq_wallet_allocation_lot"),
                    models.CheckConstraint(condition=models.Q(amount__gt=0, restored_amount__lte=models.F("amount")), name="wallet_lot_restored_valid"),
                ],
            },
        ),
    ]
