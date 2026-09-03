from django.db import migrations


PERMISSION = "order.review.manage"


def grant_permission(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        if PERMISSION not in permissions:
            permissions.append(PERMISSION)
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_permission(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.filter(code="platform-admin"):
        role.permissions = [
            permission for permission in (role.permissions or []) if permission != PERMISSION
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0016_grant_task_center_permissions")]
    operations = [migrations.RunPython(grant_permission, revoke_permission)]
