from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0020_grant_provider_order_finance_permissions")]

    operations = [
        migrations.AddField(
            model_name="providerorderaftersalescase",
            name="evidence_object_keys",
            field=models.JSONField(blank=True, default=list, verbose_name="凭证对象键"),
        ),
    ]
