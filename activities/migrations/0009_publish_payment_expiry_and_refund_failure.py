from datetime import timedelta

from django.db import migrations, models
from django.db.models import Count, Q
from django.utils import timezone


def backfill_publish_payment_deadlines(apps, schema_editor):
    PublishOrder = apps.get_model("activities", "ActivityPublishOrder")
    PublishOrder.objects.filter(expires_at__isnull=True).update(
        expires_at=models.F("created_at") + timedelta(minutes=30)
    )

    duplicates = (
        PublishOrder.objects.filter(status="pending_payment")
        .values("activity_id", "payer_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )
    now = timezone.now()
    for duplicate in duplicates.iterator():
        pending_ids = list(
            PublishOrder.objects.filter(
                status="pending_payment",
                activity_id=duplicate["activity_id"],
                payer_id=duplicate["payer_id"],
            )
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        )
        PublishOrder.objects.filter(id__in=pending_ids[1:]).update(
            status="cancelled",
            closed_at=now,
        )


class Migration(migrations.Migration):
    dependencies = [("activities", "0008_activitysettlement")]

    operations = [
        migrations.AddField(
            model_name="activityparticipationrefundorder",
            name="failure_reason",
            field=models.CharField(blank=True, max_length=1000, verbose_name="失败原因"),
        ),
        migrations.AlterField(
            model_name="activityparticipationrefundorder",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "待退款"),
                    ("processing", "退款处理中"),
                    ("succeeded", "退款成功"),
                    ("failed", "退款失败"),
                ],
                default="pending",
                max_length=20,
                verbose_name="退款状态",
            ),
        ),
        migrations.AddField(
            model_name="activitypublishorder",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="关闭时间"),
        ),
        migrations.AddField(
            model_name="activitypublishorder",
            name="expires_at",
            field=models.DateTimeField(null=True, verbose_name="支付失效时间"),
        ),
        migrations.RunPython(
            backfill_publish_payment_deadlines,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="activitypublishorder",
            name="expires_at",
            field=models.DateTimeField(verbose_name="支付失效时间"),
        ),
        migrations.AddIndex(
            model_name="activitypublishorder",
            index=models.Index(
                fields=["status", "expires_at"],
                name="activity_pub_status_exp_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="activitypublishorder",
            constraint=models.UniqueConstraint(
                condition=Q(status="pending_payment"),
                fields=("activity", "payer"),
                name="uniq_pending_activity_publish_payment",
            ),
        ),
    ]
