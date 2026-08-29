import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0004_alter_activitypublishorder_activity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="activity",
            name="city_code",
            field=models.CharField(blank=True, max_length=20, verbose_name="活动城市编码"),
        ),
        migrations.AddField(
            model_name="activity",
            name="city_name",
            field=models.CharField(blank=True, max_length=50, verbose_name="活动城市"),
        ),
        migrations.AddField(
            model_name="activity",
            name="rejection_reason",
            field=models.CharField(blank=True, max_length=500, verbose_name="驳回原因"),
        ),
        migrations.AddField(
            model_name="activity",
            name="reviewed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="审核时间"),
        ),
        migrations.AddField(
            model_name="activity",
            name="reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reviewed_activities",
                to=settings.AUTH_USER_MODEL,
                verbose_name="审核人",
            ),
        ),
        migrations.AlterField(
            model_name="activity",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "草稿"),
                    ("pending_review", "待审核"),
                    ("rejected", "已驳回"),
                    ("recruiting", "报名中"),
                    ("formed", "已成局"),
                    ("in_progress", "进行中"),
                    ("completed", "已完成"),
                    ("cancelled", "已取消"),
                    ("failed_to_form", "未成局"),
                ],
                default="draft",
                max_length=20,
                verbose_name="状态",
            ),
        ),
    ]
