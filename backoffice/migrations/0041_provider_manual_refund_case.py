import backoffice.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("backoffice", "0040_fulfillment_after_sales"),
        ("orders", "0031_distribution_safety_diagnostics"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.CreateModel(
        name="ProviderManualRefundCase",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("case_no", models.CharField(default=backoffice.models.generate_manual_refund_case_no, editable=False, max_length=24, unique=True)),
            ("request_key", models.UUIDField(unique=True)),
            ("requested_amount", models.PositiveIntegerField()),
            ("reason", models.CharField(max_length=1000)),
            ("status", models.CharField(choices=[("pending", "待财务处理"), ("processing", "财务处理中"), ("reviewing", "待财务复核"), ("completed", "人工处理已核验"), ("rejected", "已驳回·未退款")], default="pending", max_length=20)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("receipt_no", models.CharField(blank=True, max_length=128, null=True, unique=True)),
            ("evidence_reference", models.CharField(blank=True, max_length=500)),
            ("handling_note", models.CharField(blank=True, max_length=1000)),
            ("review_note", models.CharField(blank=True, max_length=1000)),
            ("paid_at", models.DateTimeField(blank=True, null=True)),
            ("closed_at", models.DateTimeField(blank=True, null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="manual_refund_cases", to="orders.providerorder")),
            ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="created_manual_refunds", to=settings.AUTH_USER_MODEL)),
            ("processed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="processed_manual_refunds", to=settings.AUTH_USER_MODEL)),
            ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_manual_refunds", to=settings.AUTH_USER_MODEL)),
        ],
        options={"ordering": ("-created_at", "-id"), "constraints": [
            models.CheckConstraint(condition=models.Q(requested_amount__gt=0), name="manual_refund_positive"),
            models.UniqueConstraint(condition=models.Q(status__in=("pending", "processing", "reviewing")), fields=("order",), name="one_open_manual_refund"),
        ]},
    )]
