from django.db import migrations, models


def grant_platform_review(apps, schema_editor):
    for role in apps.get_model("backoffice", "AdminRole").objects.filter(code="platform-admin"):
        permissions = list(role.permissions or [])
        if "order.fulfillment.review" not in permissions:
            role.permissions = [*permissions, "order.fulfillment.review"]
            role.save(update_fields=("permissions",))


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0031_receivingwithdrawalsetting")]
    operations = [
        migrations.AddField(model_name="platformoperationsetting", name="provider_order_early_tolerance_minutes", field=models.PositiveSmallIntegerField(default=30)),
        migrations.AddField(model_name="platformoperationsetting", name="provider_order_late_tolerance_minutes", field=models.PositiveSmallIntegerField(default=30)),
        migrations.RunPython(grant_platform_review, migrations.RunPython.noop),
    ]
