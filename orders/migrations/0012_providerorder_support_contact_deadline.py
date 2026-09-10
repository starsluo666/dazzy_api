import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0011_providerorderpaymentorder_providerorderrefundorder_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorder",
            name="support_contact_deadline_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="客服有效联系截止时间"
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="support_contacted_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="客服有效联系时间"
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="support_contacted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contacted_provider_orders",
                to=settings.AUTH_USER_MODEL,
                verbose_name="有效联系登记人",
            ),
        ),
        migrations.AddIndex(
            model_name="providerorder",
            index=models.Index(
                fields=["status", "support_contact_deadline_at"],
                name="provider_order_support_due_idx",
            ),
        ),
    ]
