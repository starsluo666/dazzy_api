from django.db import migrations


PERMISSIONS = ("wallet.view", "wallet.manage")


def grant_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(organization__organization_type="platform", data_scope="all"):
        permissions = list(role.permissions or [])
        if "*" in permissions or "order.finance.view" not in permissions:
            continue
        changed = False
        for permission in PERMISSIONS:
            if permission == "wallet.manage" and not (
                "order.finance.manage" in permissions or "operations.manage" in permissions
            ):
                continue
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.all():
        permissions = [item for item in (role.permissions or []) if item not in PERMISSIONS]
        if permissions != (role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0027_grant_growth_permissions")]
    operations = [migrations.RunPython(grant_permissions, revoke_permissions)]
