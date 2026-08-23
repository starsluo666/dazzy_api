from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0002_providerorder_trend_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorder",
            name="accepted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="达人接单时间"),
        ),
    ]
