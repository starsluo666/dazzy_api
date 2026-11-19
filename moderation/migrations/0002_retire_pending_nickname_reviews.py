from django.db import migrations
from django.db.models import F
from django.utils import timezone


def retire_pending_nicknames(apps, schema_editor):
    review = apps.get_model("moderation", "ContentReview")
    review.objects.using(schema_editor.connection.alias).filter(
        kind="nickname", is_current=True, status__in=("queued", "processing", "manual"),
    ).update(
        is_current=False, status="superseded", revision=F("revision") + 1,
        lease_token=None, lease_until=None, next_attempt_at=None,
        reason="昵称已改为敏感词自动校验，请重新保存昵称。", updated_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [("moderation", "0001_initial")]
    operations = [migrations.RunPython(retire_pending_nicknames, migrations.RunPython.noop)]
