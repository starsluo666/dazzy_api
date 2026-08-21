import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("activities", "0003_activitypublishorder")]

    operations = [
        migrations.AlterField(
            model_name="activitypublishorder",
            name="activity",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="publish_orders",
                to="activities.activity",
                verbose_name="活动",
            ),
        ),
    ]
