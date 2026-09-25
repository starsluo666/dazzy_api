from django.db import migrations


PERMISSIONS = ("coupon.view", "coupon.issue")


def grant_permissions(apps, schema_editor):
    AdminRole = apps.get_model("backoffice", "AdminRole")
    for role in AdminRole.objects.all():
        permissions = list(role.permissions or [])
        if "*" in permissions or "support.case.manage" not in permissions:
            continue
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
    for role in AdminRole.objects.all():
        permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in PERMISSIONS
        ]
        if permissions != (role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0024_platformoperationsetting_provider_commission_reset_period_and_more")]
    operations = [migrations.RunPython(grant_permissions, revoke_permissions)]
