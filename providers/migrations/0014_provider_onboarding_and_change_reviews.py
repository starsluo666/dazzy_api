import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q


def backfill_provider_review_data(apps, schema_editor):
    ProviderProfile = apps.get_model("providers", "ProviderProfile")
    ProviderService = apps.get_model("providers", "ProviderService")
    ProviderCategoryGrant = apps.get_model("providers", "ProviderCategoryGrant")

    for profile in ProviderProfile.objects.select_related("user").iterator():
        profile.display_name = profile.user.nickname
        profile.application_real_name = profile.identity_real_name
        profile.application_birth_date = profile.user.birth_date
        has_service = ProviderService.objects.filter(
            provider_id=profile.pk,
            is_active=True,
            category__is_active=True,
        ).exists()
        if (
            profile.status == "approved"
            and profile.identity_status == "verified"
            and bool(profile.bio.strip())
            and profile.lifestyle_photo_id
            and profile.service_city_code
            and profile.service_city_name
            and has_service
        ):
            profile.onboarding_status = "approved"
        profile.save(
            update_fields=(
                "display_name",
                "application_real_name",
                "application_birth_date",
                "onboarding_status",
            )
        )

    grants = []
    seen = set()
    for provider_id, category_id in ProviderService.objects.values_list(
        "provider_id", "category_id"
    ).iterator():
        key = (provider_id, category_id)
        if key in seen:
            continue
        seen.add(key)
        grants.append(
            ProviderCategoryGrant(
                provider_id=provider_id,
                category_id=category_id,
                is_active=True,
            )
        )
    ProviderCategoryGrant.objects.bulk_create(grants, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("mediafiles", "0001_initial"),
        ("providers", "0013_providerprofile_identity_back_photo_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="servicecategory",
            name="hourly_min_price_amount",
            field=models.PositiveBigIntegerField(
                default=1,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="按小时最低价格（分）",
            ),
        ),
        migrations.AddField(
            model_name="servicecategory",
            name="hourly_max_price_amount",
            field=models.PositiveBigIntegerField(
                default=10000000,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="按小时最高价格（分）",
            ),
        ),
        migrations.AddField(
            model_name="servicecategory",
            name="per_session_min_price_amount",
            field=models.PositiveBigIntegerField(
                default=1,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="按次最低价格（分）",
            ),
        ),
        migrations.AddField(
            model_name="servicecategory",
            name="per_session_max_price_amount",
            field=models.PositiveBigIntegerField(
                default=10000000,
                validators=[django.core.validators.MinValueValidator(1)],
                verbose_name="按次最高价格（分）",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicecategory",
            constraint=models.CheckConstraint(
                condition=Q(hourly_min_price_amount__lte=F("hourly_max_price_amount")),
                name="provider_category_hourly_price_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicecategory",
            constraint=models.CheckConstraint(
                condition=Q(
                    per_session_min_price_amount__lte=F("per_session_max_price_amount")
                ),
                name="provider_category_session_price_range",
            ),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="application_real_name",
            field=models.CharField(blank=True, max_length=50, verbose_name="申请真实姓名"),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="application_birth_date",
            field=models.DateField(blank=True, null=True, verbose_name="申请出生日期"),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="display_name",
            field=models.CharField(blank=True, max_length=30, verbose_name="达人名称"),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="onboarding_status",
            field=models.CharField(
                choices=[
                    ("incomplete", "待完善"),
                    ("pending_review", "待开通审核"),
                    ("approved", "已开通"),
                    ("rejected", "开通审核未通过"),
                ],
                default="incomplete",
                max_length=24,
                verbose_name="开通审核状态",
            ),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="onboarding_submitted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="开通审核提交时间"),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="onboarding_reviewed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="开通审核时间"),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="onboarding_reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="reviewed_provider_onboardings",
                to=settings.AUTH_USER_MODEL,
                verbose_name="开通审核人",
            ),
        ),
        migrations.AddField(
            model_name="providerprofile",
            name="onboarding_rejection_reason",
            field=models.CharField(blank=True, max_length=500, verbose_name="开通审核驳回原因"),
        ),
        migrations.CreateModel(
            name="ProviderCategoryGrant",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(default=True, verbose_name="有效")),
                ("granted_at", models.DateTimeField(auto_now_add=True, verbose_name="授权时间")),
                ("revoked_at", models.DateTimeField(blank=True, null=True, verbose_name="撤销时间")),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="provider_grants", to="providers.servicecategory", verbose_name="服务分类")),
                ("granted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="granted_provider_categories", to=settings.AUTH_USER_MODEL, verbose_name="授权人")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="category_grants", to="providers.providerprofile", verbose_name="达人")),
            ],
            options={
                "verbose_name": "达人服务分类授权",
                "verbose_name_plural": "达人服务分类授权",
                "db_table": "provider_category_grant",
            },
        ),
        migrations.AddConstraint(
            model_name="providercategorygrant",
            constraint=models.UniqueConstraint(fields=("provider", "category"), name="uniq_provider_category_grant"),
        ),
        migrations.CreateModel(
            name="ProviderProfileRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("display_name", models.CharField(max_length=30, verbose_name="达人名称")),
                ("bio", models.TextField(verbose_name="个人简介")),
                ("service_city_code", models.CharField(max_length=20, verbose_name="服务城市编码")),
                ("service_city_name", models.CharField(max_length=50, verbose_name="服务城市")),
                ("max_service_radius_km", models.PositiveSmallIntegerField(default=10, validators=[django.core.validators.MinValueValidator(10), django.core.validators.MaxValueValidator(70)], verbose_name="最大服务半径（公里）")),
                ("status", models.CharField(choices=[("pending", "待审核"), ("approved", "已通过"), ("rejected", "已驳回")], default="pending", max_length=16, verbose_name="审核状态")),
                ("submitted_at", models.DateTimeField(auto_now_add=True, verbose_name="提交时间")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True, verbose_name="审核时间")),
                ("rejection_reason", models.CharField(blank=True, max_length=500, verbose_name="驳回原因")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("lifestyle_photo", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="provider_profile_revisions", to="mediafiles.mediaasset", verbose_name="生活照")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="profile_revisions", to="providers.providerprofile", verbose_name="达人")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_provider_profile_revisions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "达人资料修订",
                "verbose_name_plural": "达人资料修订",
                "db_table": "provider_profile_revision",
                "ordering": ("-submitted_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="providerprofilerevision",
            constraint=models.UniqueConstraint(condition=Q(status="pending"), fields=("provider",), name="uniq_pending_provider_profile_revision"),
        ),
        migrations.CreateModel(
            name="ProviderServiceRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[("create", "新增"), ("update", "修改"), ("reactivate", "重新上架")], max_length=16, verbose_name="变更类型")),
                ("billing_type", models.CharField(choices=[("hourly", "按小时"), ("per_session", "按次")], max_length=16, verbose_name="计费方式")),
                ("price_amount", models.PositiveBigIntegerField(validators=[django.core.validators.MinValueValidator(1)], verbose_name="价格（分）")),
                ("estimated_duration_minutes", models.PositiveIntegerField(blank=True, null=True, verbose_name="预计服务时长（分钟）")),
                ("description", models.TextField(blank=True, verbose_name="服务说明")),
                ("status", models.CharField(choices=[("pending", "待审核"), ("approved", "已通过"), ("rejected", "已驳回")], default="pending", max_length=16, verbose_name="审核状态")),
                ("submitted_at", models.DateTimeField(auto_now_add=True, verbose_name="提交时间")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True, verbose_name="审核时间")),
                ("rejection_reason", models.CharField(blank=True, max_length=500, verbose_name="驳回原因")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="provider_service_revisions", to="providers.servicecategory", verbose_name="分类")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="service_revisions", to="providers.providerprofile", verbose_name="达人")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_provider_service_revisions", to=settings.AUTH_USER_MODEL)),
                ("service", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="revisions", to="providers.providerservice", verbose_name="原服务")),
            ],
            options={
                "verbose_name": "达人服务修订",
                "verbose_name_plural": "达人服务修订",
                "db_table": "provider_service_revision",
                "ordering": ("-submitted_at", "-id"),
            },
        ),
        migrations.AddConstraint(
            model_name="providerservicerevision",
            constraint=models.UniqueConstraint(condition=Q(status="pending"), fields=("provider", "category", "billing_type"), name="uniq_pending_provider_service_revision"),
        ),
        migrations.RunPython(backfill_provider_review_data, migrations.RunPython.noop),
    ]
