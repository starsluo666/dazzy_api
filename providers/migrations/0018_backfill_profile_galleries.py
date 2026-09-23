from django.db import migrations


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    for source, target, parent in (
        ("ProviderProfile", "ProviderProfileMedia", "provider_id"),
        ("ProviderProfileRevision", "ProviderProfileRevisionMedia", "revision_id"),
    ):
        rows = (
            apps.get_model("providers", source)
            .objects.using(alias)
            .exclude(lifestyle_photo_id=None)
            .values_list("pk", "lifestyle_photo_id")
            .iterator(chunk_size=1000)
        )
        model = apps.get_model("providers", target)
        batch = []
        for pk, asset_id in rows:
            batch.append(model(**{parent: pk, "asset_id": asset_id, "position": 0}))
            if len(batch) == 1000:
                model.objects.using(alias).bulk_create(batch, ignore_conflicts=True)
                batch = []
        if batch:
            model.objects.using(alias).bulk_create(batch, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("providers", "0017_providerprofilemedia_providerprofilerevisionmedia")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
