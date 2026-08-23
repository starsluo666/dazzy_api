from django.db import migrations


MANAGEMENT_PERMISSIONS = (
    "user.view",
    "user.status.manage",
    "user.risk.manage",
    "provider.view",
    "provider.manage",
    "provider.credit.adjust",
)


def grant_management_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        changed = False
        for permission in MANAGEMENT_PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_management_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in MANAGEMENT_PERMISSIONS
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0004_userriskflag_providercreditadjustment")]

    operations = [
        migrations.RunPython(
            grant_management_permissions,
            reverse_code=revoke_management_permissions,
        )
    ]
