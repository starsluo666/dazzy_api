from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0039_grant_coupon_campaign_permissions")]
    operations = [
        migrations.AddField(
            model_name="providerorderaftersalescase", name="fulfillment_snapshot",
            field=models.JSONField(default=dict, blank=True, verbose_name="履约异常售后核查及裁定快照"),
        ),
        migrations.AlterField(
            model_name="providerorderaftersalescase", name="case_type",
            field=models.CharField(max_length=24, verbose_name="售后类型", choices=[
                ("refund", "退款申请"), ("early_termination", "提前终止服务"),
                ("fulfillment_dispute", "履约异常售后"), ("service_dispute", "服务争议"),
                ("provider_cancel", "达人取消"), ("other", "其他售后"),
            ]),
        ),
    ]
