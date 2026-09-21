from django.db import migrations


PRICE_RANGES = {
    "travel": (10_000, 50_000, 30_000, 300_000),
    "billiards": (10_000, 30_000, 20_000, 150_000),
    "mahjong": (10_000, 30_000, 20_000, 150_000),
    "board-games": (10_000, 30_000, 15_000, 150_000),
    "business": (15_000, 80_000, 50_000, 500_000),
    "review-qa-service": (10_000, 50_000, 30_000, 300_000),
}


def configure_price_ranges(apps, schema_editor):
    ServiceCategory = apps.get_model("providers", "ServiceCategory")
    for slug, (
        hourly_min,
        hourly_max,
        per_session_min,
        per_session_max,
    ) in PRICE_RANGES.items():
        ServiceCategory.objects.filter(slug=slug).update(
            hourly_min_price_amount=hourly_min,
            hourly_max_price_amount=hourly_max,
            per_session_min_price_amount=per_session_min,
            per_session_max_price_amount=per_session_max,
        )


def reset_price_ranges(apps, schema_editor):
    ServiceCategory = apps.get_model("providers", "ServiceCategory")
    ServiceCategory.objects.filter(slug__in=PRICE_RANGES).update(
        hourly_min_price_amount=1,
        hourly_max_price_amount=10_000_000,
        per_session_min_price_amount=1,
        per_session_max_price_amount=10_000_000,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("providers", "0014_provider_onboarding_and_change_reviews"),
    ]

    operations = [
        migrations.RunPython(configure_price_ranges, reset_price_ranges),
    ]
