from django.db import migrations


def grant_permission(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        if "operations.manage" not in permissions:
            permissions.append("operations.manage")
            role.permissions = permissions
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0013_providerorderingsetting")]
    operations = [migrations.RunPython(grant_permission, migrations.RunPython.noop)]
