from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0027_provider_settlement_plan")]

    # Empty for existing rows: no invented fee evidence or historical repricing.
    operations = [
        migrations.AddField(
            model_name="providerordersettlementplan",
            name="fee_policy_snapshot",
            field=models.JSONField(default=dict, verbose_name="平台手续费承担规则快照"),
        ),
    ]
