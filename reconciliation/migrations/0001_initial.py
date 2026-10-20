import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="ReconciliationBatch", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("scope", models.CharField(max_length=64)),
            ("data_date", models.DateField()),
            ("bill_type", models.CharField(choices=[("TRADE_BILL", "交易及退款账单"), ("SPLIT_BILL", "分账账单"), ("WITHDRAWAL_BILL", "出金账单"), ("SETTLE_BILL", "商户结算账单"), ("SETTLE_USER_BILL", "用户结算账单")], max_length=32)),
            ("status", models.CharField(choices=[("pending", "待对账"), ("running", "对账中"), ("waiting", "账单未就绪"), ("failed", "对账未完成"), ("matched", "已核对项一致"), ("differences", "存在差异或待核查项")], default="pending", max_length=16)),
            ("attempts_date", models.DateField(null=True)), ("attempts", models.PositiveSmallIntegerField(default=0)),
            ("run_token", models.UUIDField(null=True)), ("lease_until", models.DateTimeField(null=True)),
            ("next_attempt_at", models.DateTimeField(null=True)), ("checked_at", models.DateTimeField(null=True)),
            ("error_code", models.CharField(blank=True, max_length=40)), ("summary", models.JSONField(default=dict)),
            ("evidence_digest", models.CharField(blank=True, max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True)),
        ], options={"indexes": [models.Index(fields=["status", "next_attempt_at"], name="recon_due_idx")],
                    "constraints": [models.UniqueConstraint(fields=("scope", "data_date", "bill_type"), name="unique_daily_channel_bill")]}),
        migrations.CreateModel(name="ReconciliationIssue", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("issue_key", models.CharField(max_length=64)), ("category", models.CharField(max_length=40)),
            ("reference", models.CharField(blank=True, max_length=128)), ("message", models.CharField(max_length=250)),
            ("evidence", models.JSONField(default=dict)), ("fingerprint", models.CharField(max_length=64)),
            ("status", models.CharField(choices=[("open", "待核查"), ("processing", "核查中"), ("review", "待复核"), ("resolved", "人工核查已结案"), ("cleared", "重新对账已一致")], default="open", max_length=16)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True)),
            ("batch", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="issues", to="reconciliation.reconciliationbatch")),
            ("submitted_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL)),
        ], options={"indexes": [models.Index(fields=["status", "created_at"], name="recon_issue_work_idx")],
                    "constraints": [models.UniqueConstraint(fields=("batch", "issue_key"), name="unique_bill_difference")]}),
        migrations.CreateModel(name="ReconciliationIssueEvent", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("action", models.CharField(max_length=32)), ("note", models.CharField(blank=True, max_length=1000)),
            ("evidence_reference", models.CharField(blank=True, max_length=200)), ("revision", models.PositiveIntegerField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("actor", models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ("issue", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="events", to="reconciliation.reconciliationissue")),
        ]),
    ]
