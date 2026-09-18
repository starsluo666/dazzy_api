from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0012_providerorder_support_contact_deadline"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_merchant_id",
            field=models.CharField(blank=True, max_length=32, verbose_name="汇付商户号"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_response_code",
            field=models.CharField(blank=True, max_length=32, verbose_name="汇付响应码"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_response_digest",
            field=models.CharField(blank=True, max_length=64, verbose_name="汇付响应摘要"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="payment_jump_url",
            field=models.URLField(blank=True, max_length=512, verbose_name="汇付支付跳转地址"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="pre_order_id",
            field=models.CharField(blank=True, max_length=64, verbose_name="汇付预下单号"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="preorder_attempts",
            field=models.PositiveSmallIntegerField(default=0, verbose_name="预下单尝试次数"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="preorder_ready_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="预下单完成时间"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="preorder_requested_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="预下单请求时间"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="preorder_status",
            field=models.CharField(
                choices=[
                    ("not_started", "未发起"),
                    ("submitting", "预下单处理中"),
                    ("ready", "预下单成功"),
                    ("failed", "预下单失败"),
                ],
                default="not_started",
                max_length=16,
                verbose_name="汇付预下单状态",
            ),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="req_date",
            field=models.CharField(blank=True, max_length=8, verbose_name="汇付请求日期"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="req_seq_id",
            field=models.CharField(blank=True, max_length=64, verbose_name="汇付请求流水号"),
        ),
        migrations.AddIndex(
            model_name="providerorderpaymentorder",
            index=models.Index(
                fields=["preorder_status", "-updated_at"],
                name="provider_pay_preorder_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerorderpaymentorder",
            constraint=models.UniqueConstraint(
                condition=~Q(req_seq_id=""),
                fields=("req_date", "req_seq_id"),
                name="uniq_provider_huifu_request",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerorderpaymentorder",
            constraint=models.UniqueConstraint(
                condition=~Q(pre_order_id=""),
                fields=("pre_order_id",),
                name="uniq_provider_huifu_preorder",
            ),
        ),
    ]
