import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_wechatminiprogramidentity"),
    ]

    operations = [
        migrations.CreateModel(
            name="WechatLoginIdentity",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("official_account", "微信内网页"),
                            ("mobile_app", "原生 App"),
                        ],
                        max_length=24,
                        verbose_name="登录渠道",
                    ),
                ),
                ("app_id", models.CharField(max_length=32, verbose_name="微信 AppID")),
                ("openid", models.CharField(max_length=128, verbose_name="微信 OpenID")),
                (
                    "unionid",
                    models.CharField(blank=True, max_length=128, verbose_name="微信 UnionID"),
                ),
                ("authorized_at", models.DateTimeField(verbose_name="最近授权时间")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wechat_login_identities",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="用户",
                    ),
                ),
            ],
            options={
                "verbose_name": "微信登录身份",
                "verbose_name_plural": "微信登录身份",
                "db_table": "accounts_wechat_login_identity",
            },
        ),
        migrations.AddConstraint(
            model_name="wechatloginidentity",
            constraint=models.UniqueConstraint(
                fields=("app_id", "openid"), name="uniq_wechat_login_app_openid"
            ),
        ),
        migrations.AddConstraint(
            model_name="wechatloginidentity",
            constraint=models.UniqueConstraint(
                fields=("user", "app_id"), name="uniq_wechat_login_user_app"
            ),
        ),
    ]
