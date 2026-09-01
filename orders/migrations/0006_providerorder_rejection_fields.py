from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0005_providerorder_address_contact_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerorder",
            name="provider_rejected_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="达人拒单时间"),
        ),
        migrations.AddField(
            model_name="providerorder",
            name="provider_rejection_reason",
            field=models.CharField(blank=True, max_length=200, verbose_name="达人拒单原因"),
        ),
    ]
