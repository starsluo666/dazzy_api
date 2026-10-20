from django.db import migrations


def grant(apps, schema_editor):
    Role = apps.get_model("backoffice", "AdminRole")
    for role in Role.objects.filter(code="platform-admin", data_scope="all"):
        permissions = list(role.permissions or [])
        for code in ("reconciliation.view", "reconciliation.manage", "reconciliation.review"):
            if code not in permissions:
                permissions.append(code)
        role.permissions = permissions
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0041_provider_manual_refund_case"), ("reconciliation", "0001_initial")]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
