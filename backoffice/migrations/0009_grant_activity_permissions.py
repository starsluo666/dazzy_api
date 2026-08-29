from django.db import migrations


ACTIVITY_PERMISSIONS = ("activity.view", "activity.review")


def grant_activity_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        changed = False
        for permission in ACTIVITY_PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_activity_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in ACTIVITY_PERMISSIONS
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0005_activity_review_and_city_fields"),
        ("backoffice", "0008_grant_service_category_permissions"),
    ]

    operations = [
        migrations.RunPython(
            grant_activity_permissions,
            reverse_code=revoke_activity_permissions,
        )
    ]
