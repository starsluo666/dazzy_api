from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0015_providerorderpaymentorder_aggregate_payment"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_last_query_digest",
            field=models.CharField(
                blank=True,
                max_length=64,
                verbose_name="最近查单响应摘要",
            ),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_last_query_status",
            field=models.CharField(blank=True, max_length=8, verbose_name="最近查单状态"),
        ),
        migrations.AddField(
            model_name="providerorderpaymentorder",
            name="gateway_last_queried_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="最近查单时间"),
        ),
        migrations.RemoveConstraint(
            model_name="providerorderpaymentorder",
            name="uniq_provider_huifu_preorder",
        ),
        migrations.RemoveField(
            model_name="providerorderpaymentorder",
            name="payment_invoke_type",
        ),
        migrations.RemoveField(
            model_name="providerorderpaymentorder",
            name="payment_jump_url",
        ),
        migrations.RemoveField(
            model_name="providerorderpaymentorder",
            name="pre_order_id",
        ),
    ]
