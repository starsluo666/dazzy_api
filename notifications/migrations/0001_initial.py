# Generated manually for the initial in-app notification center.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="UserNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("category", models.CharField(choices=[("support", "客服"), ("order", "订单"), ("activity", "活动"), ("system", "系统")], max_length=20, verbose_name="通知分类")),
                ("event_type", models.CharField(choices=[("support_reply", "客服回复"), ("support_result", "工单处理结果"), ("support_review_result", "工单复核结果"), ("order_payment_success", "订单支付成功"), ("order_accepted", "达人已接单"), ("order_pending_support", "订单转客服处理"), ("order_departed", "达人已出发"), ("order_started", "服务已开始"), ("order_completion_submitted", "达人提交完成"), ("order_auto_confirmed", "订单自动确认"), ("order_after_sales_started", "订单售后处理中"), ("order_after_sales_result", "订单售后结果"), ("activity_publish_submitted", "活动提交审核"), ("activity_review_result", "活动审核结果"), ("activity_signup_success", "活动报名成功"), ("activity_formed", "活动已成局"), ("activity_refund_completed", "活动退款完成"), ("activity_cancelled", "活动已取消"), ("activity_failed_to_form", "活动未成局"), ("activity_started", "活动已开始"), ("activity_completed", "活动已结束"), ("activity_after_sales_result", "活动售后结果"), ("activity_settled", "活动结算完成"), ("provider_application_result", "达人申请审核结果"), ("provider_status_changed", "达人资格变更"), ("provider_credit_changed", "达人信用分变更")], max_length=40, verbose_name="事件类型")),
                ("title", models.CharField(max_length=100, verbose_name="标题")),
                ("content", models.CharField(max_length=500, verbose_name="通知内容")),
                ("target_type", models.CharField(blank=True, max_length=32, verbose_name="关联对象类型")),
                ("target_id", models.CharField(blank=True, max_length=64, verbose_name="关联对象标识")),
                ("target_title", models.CharField(blank=True, max_length=160, verbose_name="关联对象标题")),
                ("action_text", models.CharField(blank=True, max_length=32, verbose_name="操作文案")),
                ("action_url", models.CharField(blank=True, max_length=255, verbose_name="客户端跳转地址")),
                ("dedupe_key", models.CharField(max_length=160, unique=True, verbose_name="幂等键")),
                ("read_at", models.DateTimeField(blank=True, null=True, verbose_name="已读时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to=settings.AUTH_USER_MODEL, verbose_name="接收用户")),
            ],
            options={
                "verbose_name": "站内通知",
                "verbose_name_plural": "站内通知",
                "db_table": "user_notification",
                "ordering": ("-created_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="usernotification",
            index=models.Index(fields=["recipient", "read_at", "-created_at"], name="notif_rec_read_created_idx"),
        ),
        migrations.AddIndex(
            model_name="usernotification",
            index=models.Index(fields=["recipient", "category", "-created_at"], name="notif_rec_cat_created_idx"),
        ),
    ]
