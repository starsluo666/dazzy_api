from django.db import migrations


AFTER_SALES_PERMISSIONS = (
    "order.after_sales.view",
    "order.after_sales.review",
)


def grant_after_sales_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        changed = False
        for permission in AFTER_SALES_PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_after_sales_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        role.permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in AFTER_SALES_PERMISSIONS
        ]
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0006_providerorderaftersalescase")]

    operations = [
        migrations.RunPython(
            grant_after_sales_permissions,
            reverse_code=revoke_after_sales_permissions,
        )
    ]
