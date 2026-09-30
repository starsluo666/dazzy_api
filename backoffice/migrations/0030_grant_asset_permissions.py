from django.db import migrations


ASSET_PERMISSIONS = ("asset.view", "asset.manage")


def grant_asset_permissions(apps, schema_editor):
    role_model = apps.get_model("backoffice", "AdminRole")
    for role in role_model.objects.filter(organization__organization_type="platform"):
        permissions = list(role.permissions or [])
        if "*" in permissions:
            continue
        changed = False
        if any(code in permissions for code in (
            "service_category.view", "service_category.manage",
            "activity_category.view", "activity_category.manage", "operations.manage",
        )) and "asset.view" not in permissions:
            permissions.append("asset.view")
            changed = True
        if any(code in permissions for code in (
            "service_category.manage", "activity_category.manage", "operations.manage",
        )) and "asset.manage" not in permissions:
            permissions.append("asset.manage")
            changed = True
        if changed:
            role.permissions = permissions
            role.save(update_fields=("permissions",))


def revoke_asset_permissions(apps, schema_editor):
    role_model = apps.get_model("backoffice", "AdminRole")
    for role in role_model.objects.all():
        permissions = [code for code in (role.permissions or []) if code not in ASSET_PERMISSIONS]
        if permissions != (role.permissions or []):
            role.permissions = permissions
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0029_platform_discovery_cities")]
    operations = [migrations.RunPython(grant_asset_permissions, revoke_asset_permissions)]
