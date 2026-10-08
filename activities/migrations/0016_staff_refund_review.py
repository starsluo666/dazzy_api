from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0015_activitycategory_icon_asset"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name="activityaftersalescase",
            name="requires_supervisor",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="activityaftersalescase",
            name="escalation_reason",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="activityaftersalescase",
            name="created_by_operator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="registered_activity_after_sales_cases",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
