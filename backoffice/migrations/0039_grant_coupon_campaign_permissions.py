from django.db import migrations


PERMISSIONS = ("coupon_campaign.view", "coupon_campaign.manage")


def grant_platform_role(apps, schema_editor):
    Role = apps.get_model("backoffice", "AdminRole")
    database = schema_editor.connection.alias
    # Only the built-in, platform-wide administrator receives the new grants.
    # Preserve custom roles, city scopes and every unrelated existing permission.
    for role in Role.objects.using(database).filter(
        code="platform-admin",
        is_system=True,
        data_scope="all",
        organization__organization_type="platform",
    ).iterator():
        permissions = list(role.permissions or [])
        missing = [permission for permission in PERMISSIONS if permission not in permissions]
        if missing:
            role.permissions = permissions + missing
            role.save(using=database, update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0038_provider_invite_permissions")]
    # A rollback must not revoke grants that may have existed before this repair.
    operations = [migrations.RunPython(grant_platform_role, migrations.RunPython.noop)]
