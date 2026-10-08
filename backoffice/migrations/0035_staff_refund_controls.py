from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("backoffice", "0034_admin_work_read_receipt")]
    operations = [
        migrations.AddField(
            model_name="platformoperationsetting",
            name="support_refund_single_limit",
            field=models.PositiveBigIntegerField(
                default=0, verbose_name="客服单订单退款上限（分）"
            ),
        ),
        migrations.AddField(
            model_name="platformoperationsetting",
            name="support_refund_daily_limit",
            field=models.PositiveBigIntegerField(
                default=0, verbose_name="客服每日退款审批上限（分）"
            ),
        ),
        migrations.AddField(
            model_name="providerorderaftersalescase",
            name="requires_supervisor",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="providerorderaftersalescase",
            name="escalation_reason",
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
