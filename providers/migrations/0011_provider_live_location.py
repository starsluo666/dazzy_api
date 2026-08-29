import django.contrib.gis.db.models.fields
import django.core.validators
import django.db.models.deletion
from decimal import Decimal

from django.db import migrations, models


def pause_all_providers(apps, schema_editor):
    ProviderProfile = apps.get_model("providers", "ProviderProfile")
    ProviderProfile.objects.filter(is_accepting_orders=True).update(is_accepting_orders=False)


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0010_pause_providers_without_service_location"),
    ]

    operations = [
        migrations.RunPython(pause_all_providers, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name="providerprofile",
            name="is_accepting_orders",
            field=models.BooleanField(default=False, verbose_name="已开启接单"),
        ),
        migrations.CreateModel(
            name="ProviderLiveLocation",
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
                (
                    "session_id",
                    models.UUIDField(blank=True, null=True, unique=True, verbose_name="接单会话ID"),
                ),
                (
                    "source_longitude",
                    models.DecimalField(decimal_places=7, max_digits=10, verbose_name="GCJ-02经度"),
                ),
                (
                    "source_latitude",
                    models.DecimalField(decimal_places=7, max_digits=10, verbose_name="GCJ-02纬度"),
                ),
                (
                    "position",
                    django.contrib.gis.db.models.fields.PointField(
                        geography=True, srid=4326, verbose_name="实时位置（WGS84）"
                    ),
                ),
                (
                    "accuracy_m",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=8,
                        validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                        verbose_name="定位精度（米）",
                    ),
                ),
                (
                    "speed_mps",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=8,
                        null=True,
                        validators=[django.core.validators.MinValueValidator(Decimal("0"))],
                        verbose_name="速度（米/秒）",
                    ),
                ),
                ("located_at", models.DateTimeField(verbose_name="客户端定位时间")),
                ("received_at", models.DateTimeField(verbose_name="服务端接收时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "provider",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="live_location",
                        to="providers.providerprofile",
                        verbose_name="达人",
                    ),
                ),
            ],
            options={
                "verbose_name": "达人实时位置",
                "verbose_name_plural": "达人实时位置",
                "db_table": "provider_live_location",
                "indexes": [models.Index(fields=["received_at"], name="provider_li_receive_8b19a9_idx")],
            },
        ),
        migrations.RemoveField(model_name="providerprofile", name="map_source"),
        migrations.RemoveField(model_name="providerprofile", name="service_address"),
        migrations.RemoveField(model_name="providerprofile", name="service_center"),
        migrations.RemoveField(model_name="providerprofile", name="service_location_name"),
        migrations.RemoveField(model_name="providerprofile", name="source_latitude"),
        migrations.RemoveField(model_name="providerprofile", name="source_longitude"),
    ]
