from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0025_alter_provider_payment_channel")]
    operations = [
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="wechat_payer_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="付款微信摘要"),
        )
    ]
