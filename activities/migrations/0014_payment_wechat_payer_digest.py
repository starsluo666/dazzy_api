from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("activities", "0013_activity_wallet_payment_routes")]
    operations = [
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="wechat_payer_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="付款微信摘要"),
        )
    ]
