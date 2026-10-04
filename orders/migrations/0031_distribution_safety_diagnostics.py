import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0030_provider_settlement_plan_ready")]

    operations = [
        migrations.AddField(
            model_name="providerorderdistribution", name="evidence_conflict_code",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="providerorderdistributionobservation", name="reason_code",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.CreateModel(
            name="ProviderOrderDistributionPreflight",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("deferred", "渠道预检查未通过"), ("blocked", "暂停自动分账，待人工核账"), ("registered", "已登记分账请求")], max_length=16)),
                ("reason_code", models.CharField(blank=True, max_length=64)),
                ("reason_message", models.CharField(blank=True, max_length=200)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                ("checked_at", models.DateTimeField()),
                ("next_retry_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("settlement", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="distribution_preflight", to="orders.providerordersettlement")),
            ],
            options={"db_table": "provider_distribution_preflight"},
        ),
    ]
