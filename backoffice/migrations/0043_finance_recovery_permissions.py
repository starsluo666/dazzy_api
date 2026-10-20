from django.db import migrations


def grant(apps, schema_editor):
    Role = apps.get_model("backoffice", "AdminRole")
    for role in Role.objects.filter(code="platform-admin", data_scope="all"):
        permissions = list(role.permissions or [])
        for code in ("finance.recovery.view", "finance.recovery.manage", "finance.recovery.review"):
            if code not in permissions:
                permissions.append(code)
        role.permissions = permissions
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0042_reconciliation_permissions"), ("financeops", "0002_seed_payment_jobs")]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
