from django.db import migrations


def grant(apps, schema_editor):
    Role = apps.get_model("backoffice", "AdminRole")
    for role in Role.objects.using(schema_editor.connection.alias).filter(code="platform-admin", data_scope="all"):
        permissions = list(role.permissions or [])
        for code in ("content.review.view", "content.review.manage"):
            if code not in permissions:
                permissions.append(code)
        role.permissions = permissions
        role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0043_finance_recovery_permissions"), ("moderation", "0001_initial")]
    operations = [migrations.RunPython(grant, migrations.RunPython.noop)]
