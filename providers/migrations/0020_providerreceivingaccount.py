from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("providers", "0019_servicecategory_icon_asset")]

    operations = [
        migrations.CreateModel(
            name="ProviderReceivingAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("details_ciphertext", models.TextField(editable=False, verbose_name="加密收款资料")),
                ("id_number_masked", models.CharField(max_length=32, verbose_name="脱敏身份证号")),
                ("bank_card_masked", models.CharField(max_length=32, verbose_name="脱敏银行卡号")),
                ("mobile_masked", models.CharField(max_length=20, verbose_name="脱敏联系电话")),
                ("bank_name", models.CharField(max_length=60, verbose_name="开户银行")),
                ("bank_province", models.CharField(max_length=40, verbose_name="开户省份")),
                ("bank_city", models.CharField(max_length=40, verbose_name="开户城市")),
                ("consent_version", models.CharField(max_length=32, verbose_name="资料收集告知版本")),
                ("consented_at", models.DateTimeField(verbose_name="同意收集时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="receiving_account", to="providers.providerprofile", verbose_name="达人")),
            ],
            options={
                "db_table": "provider_receiving_account",
                "verbose_name": "达人收款资料（非渠道开户）",
                "verbose_name_plural": "达人收款资料（非渠道开户）",
            },
        ),
    ]
