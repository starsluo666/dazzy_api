from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("wallets", "0003_walletpaymentallocation_wallet_released")]
    operations = [
        migrations.AddField(
            model_name="walletrechargeorder",
            name="wechat_payer_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="付款微信摘要"),
        )
    ]
