from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mediafiles", "0003_alter_mediaasset_category")]
    operations = [
        migrations.AddField(
            model_name="mediaasset", name="duration_ms",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="已核验视频时长（毫秒）"),
        ),
    ]
