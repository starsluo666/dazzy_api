import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("orders", "0023_coupontemplate_and_coupon_revocation"),
    ]
    operations = [
        migrations.CreateModel(
            name="GrowthCampaignConfig",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("newcomer_gift_enabled", models.BooleanField(default=False, verbose_name="启用新人礼包")),
                ("invitation_enabled", models.BooleanField(default=False, verbose_name="启用邀请奖励")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("first_order_reward_template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="growth_first_order_reward_configs", to="orders.coupontemplate", verbose_name="首单完成奖励券")),
                ("registration_reward_template", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="growth_registration_reward_configs", to="orders.coupontemplate", verbose_name="有效注册奖励券")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="updated_growth_campaign_configs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "拉新活动配置", "verbose_name_plural": "拉新活动配置", "db_table": "growth_campaign_config"},
        ),
        migrations.CreateModel(
            name="Invitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("public_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("invite_code_snapshot", models.CharField(max_length=36)),
                ("registered_at", models.DateTimeField()),
                ("first_order_completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("first_completed_order", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="invitation_reward", to="orders.providerorder")),
                ("first_order_reward_coupon", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="first_order_invitation_reward", to="orders.usercoupon")),
                ("invitee", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="received_invitation", to=settings.AUTH_USER_MODEL)),
                ("inviter", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="sent_invitations", to=settings.AUTH_USER_MODEL)),
                ("registration_reward_coupon", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="registration_invitation_reward", to="orders.usercoupon")),
            ],
            options={"db_table": "growth_invitation", "ordering": ("-registered_at", "-id")},
        ),
        migrations.CreateModel(
            name="NewcomerGiftItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("config", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="newcomer_gift_items", to="growth.growthcampaignconfig")),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="newcomer_gift_items", to="orders.coupontemplate")),
            ],
            options={"db_table": "growth_newcomer_gift_item", "ordering": ("position", "id")},
        ),
        migrations.CreateModel(
            name="NewcomerGiftGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("template_snapshot", models.JSONField(default=list)),
                ("granted_at", models.DateTimeField(auto_now_add=True)),
                ("coupons", models.ManyToManyField(blank=True, related_name="newcomer_gift_grants", to="orders.usercoupon")),
                ("invitation", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="newcomer_gift_grant", to="growth.invitation")),
                ("recipient", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="newcomer_gift_grant", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "growth_newcomer_gift_grant"},
        ),
        migrations.AddIndex(
            model_name="invitation",
            index=models.Index(fields=["inviter", "-registered_at"], name="growth_inviter_registered_idx"),
        ),
        migrations.AddConstraint(
            model_name="newcomergiftitem",
            constraint=models.UniqueConstraint(fields=("config", "template"), name="uniq_growth_gift_config_template"),
        ),
        migrations.AddConstraint(
            model_name="growthcampaignconfig",
            constraint=models.CheckConstraint(condition=models.Q(("id", 1)), name="growth_campaign_config_singleton"),
        ),
    ]
