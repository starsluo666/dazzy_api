"""Offline regression runner: always migrate a fresh, in-memory SpatiaLite DB.

Run: uv run python scripts/check_wechat_auth.py [unittest dotted test labels ...]
Requires the project's GIS runtime and mod_spatialite; never loads .env itself.
This is not a substitute for PostgreSQL concurrency or real WeChat device tests.
"""

import os
from pathlib import Path
import sys
import unittest


DEFAULT_LABELS = [
    "accounts.tests",
    "accounts.test_wechat_security",
    "orders.test_wechat_oauth",
    "orders.tests.ProviderOrderApiTests.test_huifu_payment_session_uses_server_amount_and_is_idempotent",
    "orders.tests.ProviderOrderApiTests.test_wechat_payment_authorization_grants_current_session_and_returns_to_order",
    "orders.tests.ProviderOrderApiTests.test_wechat_payment_authorization_rejects_non_payable_order",
    "orders.tests.ProviderOrderApiTests.test_huifu_payment_session_fails_closed_when_not_configured",
    "orders.tests.ProviderOrderApiTests.test_huifu_gateway_failure_recovers_by_query_without_repeating_create",
    "activities.tests.ActivityModelTests.test_activity_publish_wechat_authorization_returns_to_activity_cashier",
    "activities.tests.ActivityModelTests.test_activity_huifu_session_is_server_priced_and_idempotent",
    "activities.tests.ActivityModelTests.test_activity_full_balance_payment_bypasses_huifu_and_consumes_wallet",
    "activities.tests.ActivityModelTests.test_activity_mixed_payment_only_sends_external_remainder_to_huifu",
    "wallets.tests.WalletServiceTests",
    "wallets.test_wechat_payment",
]


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    # Required only to import base settings. These services are not contacted.
    for name in (
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "REDIS_HOST",
        "COS_REGION",
        "COS_BUCKET",
        "COS_BASE_URL",
        "TENCENT_CLOUD_SECRET_ID",
        "TENCENT_CLOUD_SECRET_KEY",
        "TENCENT_MAP_WEB_SERVICE_KEY",
        "TENCENT_MAP_WEB_SERVICE_SK",
    ):
        os.environ.setdefault(name, "offline-test.invalid")

    # Load GIS before sqlite3 on Windows to avoid conflicting SQLite DLL exports.
    from django.contrib.gis import gdal  # noqa: F401
    from django.conf import settings

    settings.DATABASES = {
        "default": {"ENGINE": "django.contrib.gis.db.backends.spatialite", "NAME": ":memory:"},
    }
    settings.ALLOWED_HOSTS = ["testserver"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.CELERY_BROKER_URL = "memory://"
    settings.CELERY_RESULT_BACKEND = "cache+memory://"
    settings.SPATIALITE_LIBRARY_PATH = os.getenv("SPATIALITE_LIBRARY_PATH") or (
        str(Path(settings.GDAL_LIBRARY_PATH).with_name("mod_spatialite.dll"))
        if os.name == "nt"
        else "mod_spatialite"
    )

    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection

    assert connection.vendor == "sqlite" and connection.settings_dict["NAME"] == ":memory:"
    call_command("check")
    call_command("makemigrations", check=True, dry_run=True, verbosity=1)
    call_command("migrate", interactive=False, verbosity=0)
    print(
        "Isolated in-memory SpatiaLite database migrated; no business database connection.",
        flush=True,
    )
    suite = unittest.TestLoader().loadTestsFromNames(sys.argv[1:] or DEFAULT_LABELS)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
