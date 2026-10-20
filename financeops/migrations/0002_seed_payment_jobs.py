from django.db import migrations


def seed(apps, schema_editor):
    Heartbeat = apps.get_model("financeops", "PaymentJobHeartbeat")
    for key in (
        "providers.process_income_transfers", "wallets.reconcile_pending_recharges",
        "reconciliation.daily_bills", "taskcenter.process_due_scheduled_tasks",
        "taskcenter.synchronize_scheduled_tasks",
    ):
        Heartbeat.objects.get_or_create(key=key)


class Migration(migrations.Migration):
    dependencies = [
        ("financeops", "0001_initial"),
        ("orders", "0039_providerorderdistribution_failure_reviewed_at"),
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
