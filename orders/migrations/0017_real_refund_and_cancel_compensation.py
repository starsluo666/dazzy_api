import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0016_remove_hosting_payment_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="close_query_req_date",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付关单查询日期"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="close_query_req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="汇付关单查询流水号"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="close_req_date",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付关单请求日期"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="close_req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="汇付关单请求流水号"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_close_queried_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近关单查询时间"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_close_response_code",
            field=models.CharField(blank=True, max_length=32, verbose_name="汇付关单响应码"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_close_response_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="汇付关单响应摘要"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_close_status",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付关单状态"),
        ),
        migrations.AddConstraint(
            model_name="providerorderpaymentorder",
            constraint=models.UniqueConstraint(
                condition=~models.Q(close_req_seq_id=""),
                fields=("close_req_date", "close_req_seq_id"),
                name="uniq_provider_huifu_close_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerorderpaymentorder",
            constraint=models.UniqueConstraint(
                condition=~models.Q(close_query_req_seq_id=""),
                fields=("close_query_req_date", "close_query_req_seq_id"),
                name="uniq_provider_huifu_close_query",
            ),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_last_queried_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近退款查询时间"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_last_query_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="最近退款查询响应摘要"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_last_query_status",
            field=models.CharField(blank=True, max_length=8, verbose_name="最近退款查询状态"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_merchant_id",
            field=models.CharField(blank=True, max_length=32, verbose_name="汇付商户号"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_response_code",
            field=models.CharField(blank=True, max_length=32, verbose_name="汇付响应码"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_response_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="汇付响应摘要"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="gateway_status",
            field=models.CharField(blank=True, max_length=8, verbose_name="渠道退款状态"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="req_date",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付退款请求日期"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="汇付退款请求流水号"),
        ),
        migrations.AlterField(
            model_name="providerorderrefundorder",
            name="gateway_refund_no",
            field=models.CharField(blank=True, max_length=128, verbose_name="渠道退款号"),
        ),
        migrations.AddConstraint(
            model_name="providerorderrefundorder",
            constraint=models.UniqueConstraint(
                condition=~models.Q(req_seq_id=""),
                fields=("req_date", "req_seq_id"),
                name="uniq_provider_huifu_refund_request",
            ),
        ),
        migrations.CreateModel(
            name="HuifuRefundNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_key", models.CharField(max_length=64, unique=True, verbose_name="事件幂等键")),
                ("huifu_id", models.CharField(max_length=32, verbose_name="汇付商户号")),
                ("req_date", models.CharField(max_length=8, verbose_name="退款请求日期")),
                ("req_seq_id", models.CharField(max_length=128, verbose_name="退款请求流水号")),
                ("hf_seq_id", models.CharField(blank=True, max_length=128, verbose_name="汇付退款全局流水号")),
                ("trans_type", models.CharField(blank=True, max_length=32, verbose_name="交易类型")),
                ("trans_stat", models.CharField(blank=True, max_length=8, verbose_name="退款状态")),
                ("ord_amt", models.CharField(blank=True, max_length=16, verbose_name="退款金额（元）")),
                ("payload_digest", models.CharField(max_length=64, verbose_name="通知报文摘要")),
                ("signature_verified", models.BooleanField(default=False, verbose_name="验签通过")),
                ("query_response_digest", models.CharField(blank=True, max_length=64, verbose_name="退款查询响应摘要")),
                ("status", models.CharField(choices=[("received", "已接收"), ("processed", "已处理")], default="received", max_length=16, verbose_name="处理状态")),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("refund_order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="huifu_notifications", to="orders.providerorderrefundorder", verbose_name="退款单")),
            ],
            options={
                "db_table": "huifu_refund_notification",
                "ordering": ("-received_at", "-id"),
                "indexes": [
                    models.Index(fields=["req_date", "req_seq_id"], name="huifu_ref_notify_req_idx"),
                    models.Index(fields=["status", "-received_at"], name="huifu_ref_notify_status_idx"),
                ],
            },
        ),
    ]
