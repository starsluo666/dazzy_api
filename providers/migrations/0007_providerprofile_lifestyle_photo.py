import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("mediafiles", "0001_initial"),
        ("providers", "0006_providerprofile_radius_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerprofile",
            name="lifestyle_photo",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="provider_lifestyle_profiles",
                to="mediafiles.mediaasset",
                verbose_name="生活照",
            ),
        ),
    ]
