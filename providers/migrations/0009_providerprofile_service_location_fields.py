from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0008_providerprofile_admin_order_restricted_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerprofile",
            name="service_address",
            field=models.CharField(blank=True, max_length=255, verbose_name="常驻服务地址"),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="service_location_name",
            field=models.CharField(blank=True, max_length=100, verbose_name="常驻服务地点"),
        ),
    ]
