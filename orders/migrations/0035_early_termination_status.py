from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0034_providerorder_completion_deadline_at_and_more")]
    operations = [migrations.AlterField(
        model_name="providerorder", name="status",
        field=models.CharField(max_length=24, default="pending_payment", verbose_name="订单状态", choices=[
            ("pending_payment", "待支付"), ("pending_acceptance", "待接单"),
            ("pending_support", "待客服处理"), ("pending_service", "待服务"),
            ("departed", "已出发"), ("in_service", "服务中"),
            ("pending_confirmation", "待确认"), ("pending_review", "待评价"),
            ("completed", "已完成"), ("terminated", "已提前终止"),
            ("cancelled", "已取消"), ("after_sales", "售后中"), ("refunded", "已退款"),
        ]),
    )]
