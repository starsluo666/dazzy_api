from django.db import migrations, models
from django.db.models import F, Q


def copy_existing_refund_routes(apps, schema_editor):
    ActivityRefundRecord = apps.get_model("activities", "ActivityRefundRecord")
    ActivityParticipationRefundOrder = apps.get_model(
        "activities", "ActivityParticipationRefundOrder"
    )
    ActivityRefundRecord.objects.update(external_refund_amount=F("refund_amount"))
    ActivityParticipationRefundOrder.objects.update(
        external_refund_amount=F("refund_amount")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0012_activity_service_fee_rate_activity_tags_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="activityrefundrecord",
            name="wallet_refund_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="余额退款（分）"),
        ),
        migrations.AddField(
            model_name="activityrefundrecord",
            name="external_refund_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="外部渠道退款（分）"),
        ),
        migrations.AddField(
            model_name="activityparticipationrefundorder",
            name="wallet_refund_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="余额退款（分）"),
        ),
        migrations.AddField(
            model_name="activityparticipationrefundorder",
            name="external_refund_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="外部渠道退款（分）"),
        ),
        migrations.RunPython(copy_existing_refund_routes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="activityrefundrecord",
            constraint=models.CheckConstraint(
                condition=Q(
                    refund_amount=F("wallet_refund_amount")
                    + F("external_refund_amount")
                ),
                name="activity_refund_route_matches",
            ),
        ),
        migrations.AddConstraint(
            model_name="activityparticipationrefundorder",
            constraint=models.CheckConstraint(
                condition=Q(
                    refund_amount=F("wallet_refund_amount")
                    + F("external_refund_amount")
                ),
                name="activity_participation_refund_route_matches",
            ),
        ),
        migrations.AlterField(
            model_name="activityparticipationpaymentorder",
            name="channel",
            field=models.CharField(
                choices=[
                    ("mock_wechat", "模拟微信支付"),
                    ("mock_alipay", "模拟支付宝"),
                    ("wechat", "微信支付"),
                    ("alipay", "支付宝"),
                    ("balance", "余额支付"),
                ],
                max_length=20,
                verbose_name="支付渠道",
            ),
        ),
    ]
