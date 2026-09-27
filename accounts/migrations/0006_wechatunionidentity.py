import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_wechatloginidentity")]

    operations = [
        migrations.CreateModel(
            name="WechatUnionIdentity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "unionid",
                    models.CharField(max_length=128, unique=True, verbose_name="微信 UnionID"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wechat_union_identities",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="用户",
                    ),
                ),
            ],
            options={
                "db_table": "accounts_wechat_union_identity",
                "verbose_name": "微信跨端身份归属",
                "verbose_name_plural": "微信跨端身份归属",
            },
        ),
    ]
