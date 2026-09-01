from django.db import migrations


PERMISSIONS = ("system.task.view", "system.task.retry")


def grant_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        changed = False
        for permission in PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.filter(code="platform-admin"):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in PERMISSIONS
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0015_platformoperationsetting")]
    operations = [migrations.RunPython(grant_permissions, revoke_permissions)]
