from django.db import migrations, models


def route_existing_refunds_to_external(apps, schema_editor):
    refund_model = apps.get_model("orders", "ProviderOrderRefundOrder")
    refund_model.objects.update(external_refund_amount=models.F("refund_amount"))


class Migration(migrations.Migration):
    dependencies = [("orders", "0023_coupontemplate_and_coupon_revocation")]

    operations = [
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="wallet_refund_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="余额退款（分）"),
        ),
        migrations.AddField(
            model_name="providerorderrefundorder",
            name="external_refund_amount",
            field=models.PositiveBigIntegerField(default=0, verbose_name="外部渠道退款（分）"),
        ),
        migrations.RunPython(route_existing_refunds_to_external, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="providerorderrefundorder",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("refund_amount", models.F("wallet_refund_amount") + models.F("external_refund_amount"))
                ),
                name="provider_refund_route_matches",
            ),
        ),
    ]
