from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0014_huifupaymentnotification"),
    ]

    operations = [
        migrations.AlterField(
            model_name="providerorderpaymentorder",
            name="gateway_trade_no",
            field=models.CharField(blank=True, max_length=128, verbose_name="渠道交易号"),
        ),
        migrations.AlterField(
            model_name="providerorderpaymentorder",
            name="req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="汇付请求流水号"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="payment_scene",
            field=models.CharField(blank=True, max_length=32, verbose_name="支付场景"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="trade_type",
            field=models.CharField(blank=True, max_length=16, verbose_name="汇付交易类型"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="payment_invoke_type",
            field=models.CharField(blank=True, max_length=32, verbose_name="客户端调起类型"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="payment_invoke_payload",
            field=models.JSONField(blank=True, default=dict, verbose_name="客户端调起参数"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_party_order_id",
            field=models.CharField(blank=True, max_length=64, verbose_name="渠道商户订单号"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_out_trans_id",
            field=models.CharField(blank=True, max_length=64, verbose_name="渠道交易订单号"),
        ),
        migrations.AlterField(
            model_name="huifupaymentnotification",
            name="req_seq_id",
            field=models.CharField(max_length=128, verbose_name="原请求流水号"),
        ),
        migrations.AlterField(
            model_name="huifupaymentnotification",
            name="query_req_seq_id",
            field=models.CharField(blank=True, max_length=128, verbose_name="查单定位流水号"),
        ),
        migrations.AddField(
            model_name="huifupaymentnotification",
            name="trans_type",
            field=models.CharField(blank=True, max_length=16, verbose_name="交易类型"),
        ),
        migrations.AddField(
            model_name="huifupaymentnotification",
            name="notify_type",
            field=models.CharField(blank=True, max_length=8, verbose_name="通知类型"),
        ),
    ]
