import secrets

from django.db import migrations, models

import growth.provider_invite_models


ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def shorten_codes(apps, schema_editor):
    source = apps.get_model("growth", "ProviderInviteSource")
    using = schema_editor.connection.alias
    if schema_editor.connection.vendor == "postgresql":
        # Old application workers must not insert twelve-character codes mid-upgrade.
        table = schema_editor.quote_name(source._meta.db_table)
        schema_editor.execute(f"LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE")
    query = source.objects.using(using)
    used = set(query.values_list("code", flat=True))
    if query.count() > len(ALPHABET) ** 4:
        raise RuntimeError("四位达人邀请码数量已超过可用范围。")
    for item in query.order_by("pk").iterator():
        if len(item.code) == 4:
            continue
        for _ in range(256):
            candidate = "".join(secrets.choice(ALPHABET) for _ in range(4))
            if candidate not in used:
                break
        else:
            raise RuntimeError("四位达人邀请码分配繁忙，请重试迁移。")
        query.filter(pk=item.pk).update(code=candidate, legacy_code=item.code)
        used.add(candidate)


def restore_codes(apps, schema_editor):
    source = apps.get_model("growth", "ProviderInviteSource")
    query = source.objects.using(schema_editor.connection.alias)
    for item in query.exclude(legacy_code__isnull=True).iterator():
        query.filter(pk=item.pk).update(code=item.legacy_code)


class Migration(migrations.Migration):
    dependencies = [("growth", "0002_providerinviteconfig_providerinvitesource_and_more")]
    operations = [
        migrations.AddField(
            model_name="providerinvitesource", name="legacy_code",
            field=models.CharField(blank=True, editable=False, max_length=12, null=True, unique=True),
        ),
        migrations.RunPython(shorten_codes, restore_codes),
        migrations.AlterField(
            model_name="providerinvitesource", name="code",
            field=models.CharField(default=growth.provider_invite_models.invitation_code, editable=False, max_length=4, unique=True),
        ),
    ]
