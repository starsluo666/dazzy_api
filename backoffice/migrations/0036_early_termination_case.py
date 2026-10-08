from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0035_staff_refund_controls"), ("orders", "0035_early_termination_status")]
    operations = [
        migrations.AddField(model_name="providerorderaftersalescase", name="termination_snapshot",
                            field=models.JSONField(default=dict, blank=True, verbose_name="提前终止申请及裁定快照")),
        migrations.AlterField(model_name="providerorderaftersalescase", name="case_type",
                              field=models.CharField(max_length=24, verbose_name="售后类型", choices=[
                                  ("refund", "退款申请"), ("early_termination", "提前终止服务"),
                                  ("service_dispute", "服务争议"), ("provider_cancel", "达人取消"), ("other", "其他售后"),
                              ])),
        migrations.AlterField(model_name="providerorderaftersalescase", name="status",
                              field=models.CharField(default="pending", max_length=20, verbose_name="处理状态", choices=[
                                  ("pending", "待处理"), ("processing", "处理中"), ("approved", "已同意·待退款"),
                                  ("refunded", "退款成功"), ("rejected", "已驳回"), ("resolved", "已处理·无需退款"),
                              ])),
    ]
