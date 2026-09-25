from django.db import migrations


PERMISSION = "coupon.manage"


def grant_permission(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.all():
        permissions = list(role.permissions or [])
        if "*" in permissions or "coupon.issue" not in permissions or PERMISSION in permissions:
            continue
        permissions.append(PERMISSION)
        role.permissions = permissions
        role.save(update_fields=("permissions",))


def revoke_permission(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.all():
        permissions = [item for item in (role.permissions or []) if item != PERMISSION]
        if permissions != (role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0025_grant_coupon_permissions")]
    operations = [migrations.RunPython(grant_permission, revoke_permission)]
