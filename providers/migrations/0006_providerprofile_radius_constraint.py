import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0005_providerprofile_is_accepting_orders_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="providerprofile",
            name="max_service_radius_km",
            field=models.PositiveSmallIntegerField(
                default=10,
                validators=[
                    django.core.validators.MinValueValidator(10),
                    django.core.validators.MaxValueValidator(70),
                ],
                verbose_name="最大服务半径（公里）",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    max_service_radius_km__gte=10,
                    max_service_radius_km__lte=70,
                ),
                name="provider_radius_between_10_70",
            ),
        ),
    ]
