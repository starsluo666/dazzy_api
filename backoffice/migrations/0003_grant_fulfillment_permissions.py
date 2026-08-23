from django.db import migrations


FULFILLMENT_PERMISSIONS = (
    "order.fulfillment.view",
    "order.support_note.add",
)


def grant_fulfillment_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        changed = False
        for permission in FULFILLMENT_PERMISSIONS:
            if permission not in permissions:
                permissions.append(permission)
                changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_fulfillment_permissions(apps, schema_editor):
    admin_role = apps.get_model("backoffice", "AdminRole")
    for role in admin_role.objects.filter(code="platform-admin"):
        permissions = [
            permission
            for permission in (role.permissions or [])
            if permission not in FULFILLMENT_PERMISSIONS
        ]
        role.permissions = permissions
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0002_providerordersupportnote")]

    operations = [
        migrations.RunPython(
            grant_fulfillment_permissions,
            reverse_code=revoke_fulfillment_permissions,
        )
    ]
