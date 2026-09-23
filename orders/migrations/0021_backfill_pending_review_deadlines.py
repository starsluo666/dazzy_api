from datetime import timedelta

from django.db import migrations
from django.db.models import F, Q
from django.db.models.functions import Coalesce


def backfill_deadlines(apps, schema_editor):
    ProviderOrder = apps.get_model("orders", "ProviderOrder")
    ProviderOrder.objects.filter(
        status="pending_review", review_expires_at__isnull=True,
    ).filter(
        Q(customer_confirmed_at__isnull=False) | Q(auto_confirmed_at__isnull=False)
    ).update(
        review_expires_at=Coalesce(F("customer_confirmed_at"), F("auto_confirmed_at"))
        + timedelta(days=7)
    )


class Migration(migrations.Migration):
    dependencies = [("orders", "0020_providerorder_review_expires_at_and_more")]
    operations = [migrations.RunPython(backfill_deadlines, migrations.RunPython.noop)]
