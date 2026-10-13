from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("providers", "0024_tencent_identity_verification")]
    operations = [
        migrations.AddField(
            model_name="provideridentitycheck",
            name="material_scope",
            field=models.CharField(
                choices=[("all", "整套认证材料"), ("front", "身份证人像面")],
                default="all", max_length=16,
            ),
        ),
    ]
