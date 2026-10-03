from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("backoffice", "0030_grant_asset_permissions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ReceivingWithdrawalSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("singleton_key", models.CharField(default="default", editable=False, max_length=20, unique=True)),
                ("cash_type", models.CharField(choices=[("T1", "下一工作日"), ("D1", "下一自然日")], max_length=2)),
                ("fix_amt", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("fee_rate", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("weekday_fix_amt", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("weekday_fee_rate", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("out_fee_acct_type", models.CharField(choices=[("01", "基本户"), ("02", "现金户"), ("05", "充值户")], max_length=2)),
                ("fee_bearer_id", models.CharField(editable=False, max_length=18)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "收款与提现配置", "verbose_name_plural": "收款与提现配置",
                "db_table": "backoffice_receiving_withdrawal_setting",
            },
        ),
    ]
