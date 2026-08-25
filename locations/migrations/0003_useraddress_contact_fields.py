from django.db import migrations, models


def populate_contact_fields(apps, schema_editor):
    UserAddress = apps.get_model("locations", "UserAddress")
    gender_map = {"male": "mr", "female": "ms"}
    for address in UserAddress.objects.select_related("user").iterator():
        user = address.user
        address.contact_name = (user.nickname or "").strip()[:30] or user.phone[:30]
        address.contact_gender = gender_map.get(user.gender, "")
        address.contact_phone = user.phone[:20]
        address.save(
            update_fields=("contact_name", "contact_gender", "contact_phone")
        )


class Migration(migrations.Migration):
    dependencies = [
        ("locations", "0002_useraddress_uniq_default_address_per_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="useraddress",
            name="contact_name",
            field=models.CharField(blank=True, default="", max_length=30, verbose_name="联系人"),
        ),
        migrations.AddField(
            model_name="useraddress",
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
            model_name="useraddress",
            name="contact_phone",
            field=models.CharField(blank=True, default="", max_length=20, verbose_name="联系电话"),
        ),
        migrations.RunPython(populate_contact_fields, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="useraddress",
            constraint=models.CheckConstraint(
                condition=models.Q(contact_gender__in=("", "mr", "ms")),
                name="user_address_contact_gender_valid",
            ),
        ),
    ]
