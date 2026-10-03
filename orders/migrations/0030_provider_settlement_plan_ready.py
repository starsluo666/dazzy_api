from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0029_provider_order_distribution")]

    # No funds, historical payout eligibility or old audit snapshots are changed.
    operations = [
        migrations.AlterField(
            model_name="providerordersettlementplan",
            name="status",
            field=models.CharField(
                choices=[
                    ("waiting", "等待结算条件"),
                    ("ready", "业务条件已满足"),
                    ("blocked", "待接通或核实资金链路"),
                    ("cancelled", "无需分账"),
                ],
                default="blocked",
                max_length=24,
            ),
        ),
    ]
