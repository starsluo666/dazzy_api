from django.db import migrations


PERMISSIONS = (
    "activity.manage",
    "activity_category.view",
    "activity_category.manage",
    "activity_report.view",
    "activity_report.manage",
)


def grant_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
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
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        role.permissions = [
            permission for permission in (role.permissions or [])
            if permission not in PERMISSIONS
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0006_activity_operations"),
        ("backoffice", "0009_grant_activity_permissions"),
    ]

    operations = [
        migrations.RunPython(grant_permissions, reverse_code=revoke_permissions)
    ]
