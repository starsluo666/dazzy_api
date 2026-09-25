from django.db import migrations


PERMISSIONS = ("growth.view", "growth.manage")


def grant_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.filter(organization__organization_type="platform", data_scope="all"):
        permissions = list(role.permissions or [])
        if "*" in permissions or "coupon.view" not in permissions:
            continue
        changed = False
        for permission in PERMISSIONS:
            if permission == "growth.manage" and not (
                "coupon.manage" in permissions or "operations.manage" in permissions
            ):
                continue
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.all():
        permissions = [
            permission for permission in (role.permissions or []) if permission not in PERMISSIONS
        ]
        if permissions != (role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0026_grant_coupon_manage_permission")]
    operations = [migrations.RunPython(grant_permissions, revoke_permissions)]
