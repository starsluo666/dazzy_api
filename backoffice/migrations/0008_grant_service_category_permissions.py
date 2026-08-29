from django.db import migrations


SERVICE_CATEGORY_PERMISSIONS = (
    "service_category.view",
    "service_category.manage",
)


def grant_service_category_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        changed = False
        for permission in SERVICE_CATEGORY_PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_service_category_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in SERVICE_CATEGORY_PERMISSIONS
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0007_grant_after_sales_permissions")]

    operations = [
        migrations.RunPython(
            grant_service_category_permissions,
            reverse_code=revoke_service_category_permissions,
        )
    ]
