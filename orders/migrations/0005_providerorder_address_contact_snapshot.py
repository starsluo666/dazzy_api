from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0004_providerorder_fulfillment_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorder",
            name="contact_gender",
            field=models.CharField(
                blank=True,
                choices=[("mr", "先生"), ("ms", "女士")],
                default="",
                max_length=8,
                verbose_name="联系人称谓",
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="meeting_location_name",
            field=models.CharField(
                blank=True,
                default="",
                max_length=100,
                verbose_name="集合地点名称快照",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerorder",
            constraint=models.CheckConstraint(
                condition=models.Q(contact_gender__in=("", "mr", "ms")),
                name="provider_order_contact_gender_valid",
            ),
        ),
    ]
