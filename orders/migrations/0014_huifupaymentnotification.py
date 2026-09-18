from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0013_providerorderpaymentorder_huifu_preorder"),
    ]

    operations = [
        migrations.CreateModel(
            name="HuifuPaymentNotification",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("event_key", models.CharField(max_length=64, unique=True, verbose_name="事件幂等键")),
                ("huifu_id", models.CharField(max_length=32, verbose_name="汇付商户号")),
                ("req_date", models.CharField(max_length=8, verbose_name="原请求日期")),
                ("req_seq_id", models.CharField(max_length=64, verbose_name="原请求流水号")),
                (
                    "hf_seq_id",
                    models.CharField(blank=True, max_length=128, verbose_name="汇付全局流水号"),
                ),
                ("trans_stat", models.CharField(max_length=8, verbose_name="交易状态")),
                (
                    "trans_amt",
                    models.CharField(blank=True, max_length=16, verbose_name="交易金额（元）"),
                ),
                ("payload_digest", models.CharField(max_length=64, verbose_name="通知报文摘要")),
                (
                    "signature_verified",
                    models.BooleanField(default=False, verbose_name="验签通过"),
                ),
                (
                    "query_req_date",
                    models.CharField(blank=True, max_length=8, verbose_name="查单请求日期"),
                ),
                (
                    "query_req_seq_id",
                    models.CharField(blank=True, max_length=64, verbose_name="查单请求流水号"),
                ),
                (
                    "query_response_digest",
                    models.CharField(blank=True, max_length=64, verbose_name="查单响应摘要"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("received", "已接收"), ("processed", "已处理")],
                        default="received",
                        max_length=16,
                        verbose_name="处理状态",
                    ),
                ),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "payment_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="huifu_notifications",
                        to="orders.providerorderpaymentorder",
                        verbose_name="支付单",
                    ),
                ),
            ],
            options={
                "db_table": "huifu_payment_notification",
                "ordering": ("-received_at", "-id"),
            },
        ),
        migrations.AddIndex(
            model_name="huifupaymentnotification",
            index=models.Index(
                fields=["req_date", "req_seq_id"],
                name="huifu_notify_request_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="huifupaymentnotification",
            index=models.Index(
                fields=["status", "-received_at"],
                name="huifu_notify_status_idx",
            ),
        ),
    ]
