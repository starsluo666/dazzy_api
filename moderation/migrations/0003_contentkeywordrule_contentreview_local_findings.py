from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("moderation", "0002_retire_pending_nickname_reviews")]

    operations = [
        migrations.AddField(
            model_name="contentreview",
            name="local_findings",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="ContentKeywordRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("keyword", models.CharField(max_length=60)),
                ("normalized_keyword", models.CharField(editable=False, max_length=120)),
                ("scope", models.CharField(choices=[("nickname", "用户昵称"), ("review", "评价文字"), ("both", "昵称和评价")], max_length=12)),
                ("label", models.CharField(blank=True, max_length=40)),
                ("enabled", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ("-updated_at", "-pk"),
                "constraints": [models.UniqueConstraint(fields=("scope", "normalized_keyword"), name="unique_content_keyword_scope")],
            },
        ),
    ]
