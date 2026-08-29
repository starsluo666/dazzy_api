from django.db import migrations


PERMISSION = "activity_settlement.manage"


def grant_permission(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        if PERMISSION not in permissions:
            permissions.append(PERMISSION)
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_permission(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission != PERMISSION
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0008_activitysettlement"),
        ("backoffice", "0011_grant_activity_finance_permissions"),
    ]

    operations = [
        migrations.RunPython(grant_permission, reverse_code=revoke_permission)
    ]
