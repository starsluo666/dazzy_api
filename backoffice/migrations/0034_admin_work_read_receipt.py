from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("backoffice", "0033_platformoperationsetting_provider_order_departure_grace_minutes_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [migrations.CreateModel(
        name="AdminWorkReadReceipt",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("queue_key", models.CharField(max_length=64)),
            ("object_id", models.CharField(max_length=40)),
            ("event_version", models.CharField(max_length=160)),
            ("read_at", models.DateTimeField(auto_now_add=True)),
            ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="admin_work_reads", to=settings.AUTH_USER_MODEL)),
        ],
        options={"constraints": [models.UniqueConstraint(fields=("user", "queue_key", "object_id", "event_version"), name="admin_work_read_unique")]},
    )]
