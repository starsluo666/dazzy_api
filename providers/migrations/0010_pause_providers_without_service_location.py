from django.db import migrations


def pause_providers_without_service_location(apps, schema_editor):
    ProviderProfile = apps.get_model("providers", "ProviderProfile")
    ProviderProfile.objects.filter(
        service_center__isnull=True,
        is_accepting_orders=True,
    ).update(is_accepting_orders=False)


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0009_providerprofile_service_location_fields"),
    ]

    operations = [
        migrations.RunPython(
            pause_providers_without_service_location,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
