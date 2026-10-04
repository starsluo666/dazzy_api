from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0031_distribution_safety_diagnostics")]
    operations = [
        migrations.AddField(model_name="providerorder", name="provider_contact_initiated_at", field=models.DateTimeField(blank=True, null=True, verbose_name="达人发起联系时间")),
        migrations.AddField(model_name="providerorder", name="departure_contact_confirmed_at", field=models.DateTimeField(blank=True, null=True, verbose_name="达人确认已联系核实时间")),
        migrations.AddField(model_name="providerorder", name="completion_longitude", field=models.DecimalField(blank=True, null=True, max_digits=10, decimal_places=7, verbose_name="完成GCJ-02经度")),
        migrations.AddField(model_name="providerorder", name="completion_latitude", field=models.DecimalField(blank=True, null=True, max_digits=10, decimal_places=7, verbose_name="完成GCJ-02纬度")),
        migrations.AddField(model_name="providerorder", name="completion_location_accuracy_m", field=models.DecimalField(blank=True, null=True, max_digits=8, decimal_places=2, verbose_name="完成定位精度（米）")),
        migrations.AddField(model_name="providerorder", name="fulfillment_policy", field=models.JSONField(blank=True, default=dict, verbose_name="履约时间规则快照")),
        migrations.AddField(model_name="providerorder", name="fulfillment_review_required", field=models.BooleanField(default=False, db_index=True, verbose_name="履约异常待审核")),
        migrations.AddField(model_name="providerorder", name="fulfillment_revision", field=models.PositiveIntegerField(default=0, verbose_name="履约异常版本")),
        migrations.AddField(model_name="providerorder", name="fulfillment_issues", field=models.JSONField(blank=True, default=list, verbose_name="履约异常记录")),
        migrations.AddField(model_name="providerorder", name="fulfillment_reviews", field=models.JSONField(blank=True, default=list, verbose_name="履约复核记录")),
        migrations.AddField(model_name="providerorder", name="confirmation_remaining_seconds", field=models.PositiveIntegerField(blank=True, null=True, verbose_name="暂停时剩余确认秒数")),
    ]
