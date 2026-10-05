import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def initialize_training(apps, schema_editor):
    alias = schema_editor.connection.alias
    apps.get_model("providers", "ProviderTrainingConfig").objects.using(alias).get_or_create(pk=1)
    # Only an actual, previously accepted order exempts a legacy provider.
    accepted = apps.get_model("orders", "ProviderOrder").objects.using(alias).filter(
        accepted_at__isnull=False,
    ).values("provider_id")
    apps.get_model("providers", "ProviderProfile").objects.using(alias).filter(pk__in=accepted).update(training_exempt=True)


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0022_provider_income_wallet"), ("orders", "0032_fulfillment_controls"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name="providerprofile", name="training_exempt",
            field=models.BooleanField(default=False, editable=False, verbose_name="历史已接单达人免首次学习"),
        ),
        migrations.AddField(
            model_name="providerprofile", name="training_passed_at",
            field=models.DateTimeField(blank=True, editable=False, null=True, verbose_name="首次接单考核通过时间"),
        ),
        migrations.CreateModel(
            name="ProviderTrainingVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("payload", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "provider_training_version"},
        ),
        migrations.CreateModel(
            name="ProviderTrainingConfig",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("draft", models.JSONField(default=dict)),
                ("revision", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("active_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to="providers.providertrainingversion")),
            ],
            options={"db_table": "provider_training_config"},
        ),
        migrations.CreateModel(
            name="ProviderTrainingProgress",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("completed_lesson_ids", models.JSONField(default=list)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="training_progress", to="providers.providerprofile")),
                ("version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="providers.providertrainingversion")),
            ],
            options={"db_table": "provider_training_progress", "constraints": [models.UniqueConstraint(fields=("provider", "version"), name="uniq_provider_training_progress")]},
        ),
        migrations.CreateModel(
            name="ProviderTrainingAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("answers", models.JSONField()),
                ("score", models.PositiveSmallIntegerField()),
                ("passed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="training_attempts", to="providers.providerprofile")),
                ("version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="providers.providertrainingversion")),
            ],
            options={"db_table": "provider_training_attempt", "ordering": ("-id",)},
        ),
        migrations.RunPython(initialize_training, migrations.RunPython.noop),
    ]
