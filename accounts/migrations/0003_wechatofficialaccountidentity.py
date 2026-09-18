from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_user_auth_version"),
    ]

    operations = [
        migrations.CreateModel(
            name="WechatOfficialAccountIdentity",
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
                ("app_id", models.CharField(max_length=32, verbose_name="服务号 AppID")),
                ("openid", models.CharField(max_length=128, verbose_name="服务号 OpenID")),
                ("unionid", models.CharField(blank=True, max_length=128, verbose_name="微信 UnionID")),
                ("authorized_at", models.DateTimeField(verbose_name="最近授权时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wechat_official_account_identities",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="用户",
                    ),
                ),
            ],
            options={
                "verbose_name": "微信服务号身份",
                "verbose_name_plural": "微信服务号身份",
                "db_table": "accounts_wechat_official_identity",
            },
        ),
        migrations.AddConstraint(
            model_name="wechatofficialaccountidentity",
            constraint=models.UniqueConstraint(
                fields=("user", "app_id"),
                name="uniq_wechat_official_user_app",
            ),
        ),
        migrations.AddConstraint(
            model_name="wechatofficialaccountidentity",
            constraint=models.UniqueConstraint(
                fields=("app_id", "openid"),
                name="uniq_wechat_official_app_openid",
            ),
        ),
    ]
