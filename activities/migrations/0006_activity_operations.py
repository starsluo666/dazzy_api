import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import activities.models


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0005_activity_review_and_city_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="activitycategory", name="city_codes",
            field=models.JSONField(blank=True, default=list, verbose_name="展示城市编码"),
        ),
        migrations.AddField(
            model_name="activitycategory", name="content_guidance",
            field=models.CharField(blank=True, max_length=500, verbose_name="内容规则提示"),
        ),
        migrations.AddField(
            model_name="activitycategory", name="max_aa_principal_amount",
            field=models.PositiveBigIntegerField(default=10000000, verbose_name="最高AA本金（分）"),
        ),
        migrations.AddField(
            model_name="activitycategory", name="max_capacity",
            field=models.PositiveSmallIntegerField(default=100, verbose_name="人数上限"),
        ),
        migrations.AddField(
            model_name="activitycategory", name="min_aa_principal_amount",
            field=models.PositiveBigIntegerField(default=1, verbose_name="最低AA本金（分）"),
        ),
        migrations.AddField(
            model_name="activitycategory", name="min_capacity",
            field=models.PositiveSmallIntegerField(default=2, verbose_name="最少人数下限"),
        ),
        migrations.AddField(
            model_name="activity", name="cancellation_reason",
            field=models.CharField(blank=True, max_length=500, verbose_name="取消原因"),
        ),
        migrations.AddField(
            model_name="activity", name="cancelled_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="取消时间"),
        ),
        migrations.AddField(
            model_name="activity", name="cancelled_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="cancelled_activities", to=settings.AUTH_USER_MODEL, verbose_name="取消操作人"),
        ),
        migrations.AddConstraint(
            model_name="activitycategory",
            constraint=models.CheckConstraint(condition=models.Q(("max_capacity__gte", models.F("min_capacity"))), name="activity_category_capacity_range"),
        ),
        migrations.AddConstraint(
            model_name="activitycategory",
            constraint=models.CheckConstraint(condition=models.Q(("max_aa_principal_amount__gte", models.F("min_aa_principal_amount"))), name="activity_category_amount_range"),
        ),
        migrations.CreateModel(
            name="ActivityRefundRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("refund_no", models.CharField(default=activities.models.generate_activity_refund_no, editable=False, max_length=24, unique=True, verbose_name="退款单号")),
                ("refund_type", models.CharField(choices=[("review_rejection", "审核驳回退款"), ("admin_cancellation", "后台取消退款")], max_length=24, verbose_name="退款类型")),
                ("principal_amount", models.PositiveBigIntegerField(verbose_name="AA本金退款（分）")),
                ("service_fee_amount", models.PositiveBigIntegerField(verbose_name="平台服务费退款（分）")),
                ("refund_amount", models.PositiveBigIntegerField(verbose_name="退款总额（分）")),
                ("status", models.CharField(choices=[("simulated_refunded", "模拟退款成功")], default="simulated_refunded", max_length=24, verbose_name="退款状态")),
                ("reason", models.CharField(max_length=500, verbose_name="退款原因")),
                ("refunded_at", models.DateTimeField(verbose_name="退款时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("activity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="refund_records", to="activities.activity", verbose_name="活动")),
                ("beneficiary", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="activity_refund_records", to=settings.AUTH_USER_MODEL, verbose_name="退款用户")),
                ("operator", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="operated_activity_refunds", to=settings.AUTH_USER_MODEL, verbose_name="操作人")),
                ("publish_order", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="refund_record", to="activities.activitypublishorder", verbose_name="发布支付单")),
            ],
            options={"db_table": "activity_refund_record", "ordering": ("-created_at", "-id"), "verbose_name": "活动退款记录", "verbose_name_plural": "活动退款记录"},
        ),
        migrations.AddIndex(model_name="activityrefundrecord", index=models.Index(fields=["activity", "-created_at"], name="activity_re_activit_2aae89_idx")),
        migrations.AddConstraint(model_name="activityrefundrecord", constraint=models.CheckConstraint(condition=models.Q(("refund_amount", models.F("principal_amount") + models.F("service_fee_amount"))), name="activity_refund_amount_matches_components")),
        migrations.CreateModel(
            name="ActivityReport",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("case_no", models.CharField(default=activities.models.generate_activity_report_no, editable=False, max_length=24, unique=True, verbose_name="举报单号")),
                ("reason", models.CharField(choices=[("false_information", "信息不实"), ("inappropriate_content", "内容不当"), ("private_transaction", "诱导私下交易"), ("safety_risk", "存在安全风险"), ("other", "其他问题")], max_length=32, verbose_name="举报原因")),
                ("description", models.CharField(blank=True, max_length=1000, verbose_name="补充说明")),
                ("status", models.CharField(choices=[("pending", "待处理"), ("processing", "处理中"), ("resolved", "已处理"), ("rejected", "不予受理")], default="pending", max_length=20, verbose_name="处理状态")),
                ("result_note", models.CharField(blank=True, max_length=1000, verbose_name="处理结论")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True, verbose_name="处理时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("activity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reports", to="activities.activity", verbose_name="活动")),
                ("reporter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="activity_reports", to=settings.AUTH_USER_MODEL, verbose_name="举报人")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reviewed_activity_reports", to=settings.AUTH_USER_MODEL, verbose_name="处理人")),
            ],
            options={"db_table": "activity_report", "ordering": ("-created_at", "-id"), "verbose_name": "活动举报", "verbose_name_plural": "活动举报"},
        ),
        migrations.AddIndex(model_name="activityreport", index=models.Index(fields=["status", "-created_at"], name="activity_re_status_bfcc83_idx")),
        migrations.AddIndex(model_name="activityreport", index=models.Index(fields=["activity", "status", "-created_at"], name="activity_re_activit_823ea1_idx")),
    ]
