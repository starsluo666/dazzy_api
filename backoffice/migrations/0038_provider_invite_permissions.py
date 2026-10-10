from django.db import migrations


def grant_platform_role(apps, schema_editor):
    Role = apps.get_model("backoffice", "AdminRole")
    for role in Role.objects.filter(
        code="platform-admin",
        is_system=True,
        data_scope="all",
        organization__organization_type="platform",
    ):
        role.permissions = sorted(
            set(role.permissions)
            | {
                "provider_invite.view",
                "provider_invite.manage",
                "provider_invite.review",
                "provider_invite.pay",
            }
        )
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [
        ("backoffice", "0037_platformoperationsetting_provider_cancellation_config_and_more"),
        ("growth", "0002_providerinviteconfig_providerinvitesource_and_more"),
    ]
    operations = [migrations.RunPython(grant_platform_role, migrations.RunPython.noop)]
