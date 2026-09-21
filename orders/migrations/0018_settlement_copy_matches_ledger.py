from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0017_real_refund_and_cancel_compensation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="providerordersettlement",
            name="settled_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="平台账务结算时间"),
        ),
        migrations.AlterField(
            model_name="providerordersettlement",
            name="status",
            field=models.CharField(
                choices=[
                    ("risk_frozen", "风险冻结中"),
                    ("dispute_frozen", "争议冻结中"),
                    ("settled", "平台账务已结算"),
                    ("cancelled", "已取消"),
                ],
                default="risk_frozen",
                max_length=24,
                verbose_name="结算状态",
            ),
        ),
    ]
