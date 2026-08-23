import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mediafiles", "0001_initial"),
        ("orders", "0003_providerorder_accepted_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorder",
            name="arrival_latitude",
            field=models.DecimalField(
                blank=True, decimal_places=7, max_digits=10, null=True,
                verbose_name="集合照GCJ-02纬度",
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="arrival_location_accuracy_m",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=8, null=True,
                verbose_name="集合照定位精度（米）",
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="arrival_longitude",
            field=models.DecimalField(
                blank=True, decimal_places=7, max_digits=10, null=True,
                verbose_name="集合照GCJ-02经度",
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="arrival_photo",
            field=models.OneToOneField(
                blank=True,
                limit_choices_to={"category": "order_evidence"},
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="provider_order_arrival_evidence",
                to="mediafiles.mediaasset",
                verbose_name="集合地点照片",
            ),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="arrival_photo_uploaded_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="集合照绑定时间"),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="completion_submitted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="达人提交完成时间"),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="customer_confirmed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="用户确认完成时间"),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="departed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="达人出发时间"),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="service_started_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="服务开始时间"),
        ),
    ]
