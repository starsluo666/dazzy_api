from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="providerorder",
            index=models.Index(fields=["created_at"], name="provider_order_created_idx"),
        ),
        migrations.AddIndex(
            model_name="providerorder",
            index=models.Index(
                condition=Q(("paid_at__isnull", False)),
                fields=["paid_at"],
                name="provider_order_paid_idx",
            ),
        ),
    ]
