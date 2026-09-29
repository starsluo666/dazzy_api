from django.db import migrations, models

import locations.discovery


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0028_grant_wallet_permissions")]

    operations = [
        migrations.AddField(
            model_name="platformoperationsetting",
            name="discovery_cities",
            field=models.JSONField(
                default=locations.discovery.default_discovery_cities,
                verbose_name="发现页开通城市",
            ),
        ),
    ]
