from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0024_provider_refund_payment_routes")]

    operations = [
        migrations.AlterField(
            model_name="providerorderpaymentorder",
            name="channel",
            field=models.CharField(
                choices=[
                    ("unselected", "待选择"),
                    ("balance", "余额支付"),
                    ("mixed", "余额组合支付"),
                    ("mock_wechat", "模拟微信支付"),
                    ("mock_alipay", "模拟支付宝"),
                    ("wechat", "微信支付"),
                    ("alipay", "支付宝"),
                ],
                default="unselected",
                max_length=20,
                verbose_name="支付渠道",
            ),
        )
    ]
