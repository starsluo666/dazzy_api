from django.db import migrations


PERMISSIONS = ("order.finance.view", "order.finance.manage")


def grant_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.all().iterator():
        permissions = list(role.permissions or [])
        if "*" in permissions or role.code not in ("platform_admin", "finance"):
            continue
        changed = False
        for permission in PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions", "updated_at"))


def revoke_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.all().iterator():
        permissions = [item for item in (role.permissions or []) if item not in PERMISSIONS]
        if permissions != (role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=("permissions", "updated_at"))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0019_platformoperationsetting_provider_order_settlement_freeze_days_and_more")]

    operations = [migrations.RunPython(grant_permissions, revoke_permissions)]
