import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("orders", "0032_fulfillment_controls"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="usercoupon", name="issue_request_id",
                            field=models.UUIDField(blank=True, editable=False, null=True, unique=True)),
        migrations.CreateModel(
            name="CouponIssueBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("request_id", models.UUIDField(editable=False, unique=True)),
                ("template_snapshot", models.JSONField(default=dict)),
                ("status", models.CharField(db_index=True, default="preview", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="issue_batches", to="orders.coupontemplate")),
            ], options={"db_table": "coupon_issue_batch", "ordering": ("-id",)},
        ),
        migrations.CreateModel(
            name="CouponIssueRecipient",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(default="pending", max_length=16)),
                ("error", models.CharField(blank=True, max_length=200)),
                ("batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recipients", to="orders.couponissuebatch")),
                ("coupon", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="orders.usercoupon")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ], options={
                "db_table": "coupon_issue_recipient",
                "indexes": [models.Index(fields=["batch", "status", "id"], name="coupon_batch_pending_idx")],
                "constraints": [models.UniqueConstraint(fields=("batch", "user"), name="unique_coupon_batch_user")],
            },
        ),
    ]
