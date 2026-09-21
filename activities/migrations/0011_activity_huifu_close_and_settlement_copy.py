from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("activities", "0010_activityrefundrecord_failure_reason_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="close_query_req_date",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付关单查询日期"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="close_query_req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="汇付关单查询流水号"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="close_req_date",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付关单请求日期"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="close_req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="汇付关单请求流水号"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="gateway_close_queried_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近关单查询时间"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="gateway_close_response_code",
            field=models.CharField(blank=True, max_length=32, verbose_name="汇付关单响应码"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="gateway_close_response_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="汇付关单响应摘要"),
        ),
        migrations.AddField(
            model_name="activityhuifupaymentorder",
            name="gateway_close_status",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付关单状态"),
        ),
        migrations.AlterField(
            model_name="activitysettlement",
            name="settled_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="平台账务结算时间"),
        ),
        migrations.AlterField(
            model_name="activitysettlement",
            name="status",
            field=models.CharField(
                choices=[
                    ("confirming", "履约确认中"),
                    ("risk_frozen", "风险冻结中"),
                    ("dispute_frozen", "争议冻结中"),
                    ("settled", "平台账务已结算"),
                ],
                default="confirming",
                max_length=24,
                verbose_name="结算状态",
            ),
        ),
        migrations.AddConstraint(
            model_name="activityhuifupaymentorder",
            constraint=models.UniqueConstraint(
                condition=~Q(close_req_seq_id=""),
                fields=("close_req_date", "close_req_seq_id"),
                name="uniq_act_huifu_close_req",
            ),
        ),
        migrations.AddConstraint(
            model_name="activityhuifupaymentorder",
            constraint=models.UniqueConstraint(
                condition=~Q(close_query_req_seq_id=""),
                fields=("close_query_req_date", "close_query_req_seq_id"),
                name="uniq_act_huifu_close_query",
            ),
        ),
    ]
