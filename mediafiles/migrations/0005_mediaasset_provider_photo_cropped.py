from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("mediafiles", "0004_mediaasset_duration_ms")]
    operations = [
        migrations.AddField(
            model_name="mediaasset",
            name="provider_photo_cropped",
            field=models.BooleanField(default=False, verbose_name="已完成达人照片5:4裁剪"),
        ),
    ]
