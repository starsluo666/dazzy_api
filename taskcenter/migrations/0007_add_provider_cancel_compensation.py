from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("taskcenter", "0006_add_provider_rejection_support_timeout")]

    operations = [
        migrations.AlterField(
            model_name="scheduledtask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("provider_order_payment_expiry", "达人订单支付超时"),
                    ("provider_acceptance_timeout", "达人接单超时"),
                    ("provider_rejection_support_timeout", "达人拒单客服处理超时"),
                    ("provider_order_confirmation_timeout", "达人订单确认超时"),
                    ("provider_order_settlement", "达人订单资金结算"),
                    ("provider_order_cancel_compensation", "达人订单取消支付补偿"),
                    ("provider_order_refund", "达人订单退款"),
                    ("activity_publish_payment_expiry", "活动发布支付超时"),
                    ("activity_participation_payment_expiry", "活动报名支付超时"),
                    ("activity_participation_refund", "活动报名退款"),
                    ("activity_formation_deadline", "活动成局截止"),
                    ("activity_start", "活动开始"),
                    ("activity_completion", "活动结束"),
                    ("activity_settlement", "活动资金结算"),
                ],
                max_length=64,
                verbose_name="任务类型",
            ),
        )
    ]
