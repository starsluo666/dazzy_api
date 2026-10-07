"""Run synthetic regressions in a new, disposable PostgreSQL/PostGIS database.

Credentials: JSON on stdin (host, port, dbname, user, password), never a file.
The supplied database is probed read-only, never migrated, flushed or dropped.
Requires CREATE DATABASE and permission to enable PostGIS in the new database.
TLS is mandatory unless --allow-plaintext is explicitly authorized for testing.
No .env is loaded; real outbound HTTP is blocked. All fixtures are synthetic.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys
import unittest
from unittest.mock import patch

import psycopg
from psycopg import sql


DEFAULT_LABELS = [
    "orders.test_distributions",
    "orders.test_distribution_safety",
    "orders.test_settlement_plans",
    "orders.test_settlement_fees",
    "orders.tests",
    "orders.test_huifu",
    "orders.test_huifu_gateway",
    "orders.test_payment_recovery",
    "orders.test_fulfillment",
    "orders.test_timeouts",
    "providers.test_withdrawals",
    "providers.test_receiving_onboarding",
    "providers.test_income_concurrency",
    "providers.test_huifu_user_transport",
    "wallets.tests.WalletServiceTests",
    "wallets.tests.WalletConcurrencyTests",
    "backoffice.tests",
    "taskcenter.tests",
    "accounts.test_account_closure",
]
DATABASE_MARKER = "dazzy-isolated-regression-v1: disposable synthetic fixtures only"


def run_tests(config, database, sslmode, labels):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    for name in list(os.environ):
        if name.startswith(("HUIFU_", "TENCENT_", "COS_")):
            del os.environ[name]
    for name in (
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "REDIS_HOST",
        "COS_REGION",
        "COS_BASE_URL",
        "TENCENT_CLOUD_SECRET_ID",
        "TENCENT_CLOUD_SECRET_KEY",
        "TENCENT_MAP_WEB_SERVICE_KEY",
        "TENCENT_MAP_WEB_SERVICE_SK",
    ):
        os.environ[name] = "offline-test.invalid"
    os.environ["COS_BUCKET"] = "offline-test-1250000000"

    from django.contrib.gis import gdal  # noqa: F401
    from django.conf import settings

    settings.DEBUG = False
    settings.DATABASES = {
        "default": {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "HOST": config["host"],
            "PORT": config["port"],
            "NAME": database,
            "USER": config["user"],
            "PASSWORD": config["password"],
            "CONN_MAX_AGE": 0,
            "OPTIONS": {
                "sslmode": sslmode,
                "connect_timeout": 10,
                "options": "-c lock_timeout=15000 -c statement_timeout=90000",
            },
        }
    }
    settings.ALLOWED_HOSTS = ["testserver"]
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.CELERY_BROKER_URL = "memory://"
    settings.CELERY_RESULT_BACKEND = "cache+memory://"

    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection, connections

    try:
        assert database != config["dbname"] and database.startswith("codex_regression_")
        assert connection.vendor == "postgresql"
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            assert cursor.fetchone()[0] == database
        with (
            patch(
                "requests.sessions.Session.send",
                side_effect=AssertionError("Real HTTP blocked by test runner"),
            ),
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("Real HTTP blocked by test runner"),
            ),
        ):
            call_command("check")
            call_command("makemigrations", check=True, dry_run=True, verbosity=1)
            print("Migrating isolated database: " + database, flush=True)
            call_command("migrate", interactive=False, verbosity=0)
            print("Isolated database migrated; starting PostgreSQL regressions.", flush=True)
            suite = unittest.TestLoader().loadTestsFromNames(labels or DEFAULT_LABELS)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            print(
                json.dumps(
                    {
                        "tests": result.testsRun,
                        "failures": len(result.failures),
                        "errors": len(result.errors),
                        "skipped": len(result.skipped),
                        "passed": result.testsRun
                        - len(result.failures)
                        - len(result.errors)
                        - len(result.skipped),
                    }
                ),
                flush=True,
            )
            return not result.wasSuccessful()
    finally:
        connections.close_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Read-only prerequisites only")
    parser.add_argument(
        "--allow-plaintext", action="store_true", help="Explicit test-only TLS opt-out"
    )
    parser.add_argument(
        "--cleanup-created-db", help="Explicit cleanup of a previously created disposable database"
    )
    parser.add_argument("labels", nargs="*")
    args = parser.parse_args()
    config = json.loads(sys.stdin.readline())
    if set(config) != {"host", "port", "dbname", "user", "password"}:
        raise ValueError("Expected host, port, dbname, user and password only.")
    sslmode = "disable" if args.allow_plaintext else "require"
    connection_args = dict(config, sslmode=sslmode, connect_timeout=10)
    with psycopg.connect(
        **connection_args,
        options="-c default_transaction_read_only=on -c statement_timeout=10000",
    ) as probe:
        db, role, version, create_db, superuser = probe.execute(
            "SELECT current_database(), current_user, version(), rolcreatedb, rolsuper "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        extensions = probe.execute(
            "SELECT name, default_version, installed_version "
            "FROM pg_available_extensions WHERE name = %s",
            ("postgis",),
        ).fetchall()
        temporary_databases = probe.execute(
            "SELECT datname FROM pg_database WHERE starts_with(datname, %s) ORDER BY datname",
            ("codex_regression_",),
        ).fetchall()
        assert db == config["dbname"]
        print(
            json.dumps(
                {
                    "database": db,
                    "role": role,
                    "server_version": version,
                    "create_database": create_db,
                    "superuser": superuser,
                    "postgis": extensions,
                    "tls": probe.pgconn.ssl_in_use,
                    "existing_regression_databases": [row[0] for row in temporary_databases],
                }
            ),
            flush=True,
        )
    if args.probe:
        return 0
    if args.cleanup_created_db:
        database = args.cleanup_created_db
        if database == config["dbname"] or not re.fullmatch(
            r"codex_regression_\d{8}_\d{6}_[0-9a-f]{8}", database
        ):
            raise ValueError(
                "Cleanup only accepts an exact disposable database name, never the supplied database."
            )
        with psycopg.connect(**connection_args, autocommit=True) as cleanup:
            row = cleanup.execute(
                "SELECT d.oid, r.rolname, shobj_description(d.oid, 'pg_database') "
                "FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE d.datname=%s",
                (database,),
            ).fetchone()
            if row is None or row[1] != role or row[2] != DATABASE_MARKER:
                raise RuntimeError(
                    "Cleanup database absent, wrong owner or missing runner marker; no deletion attempted."
                )
            # Only run this branch with a name from this runner's creation log.
            # Never enumerate/drop by prefix and never terminate other sessions.
            cleanup.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
            print("Removed explicitly selected test database: " + database, flush=True)
        return 0
    if not (create_db or superuser) or not extensions:
        raise RuntimeError("Isolated database prerequisites missing; supplied database untouched.")

    database = (
        "codex_regression_"
        + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_")
        + secrets.token_hex(4)
    )
    created = False
    identity = None
    with psycopg.connect(**connection_args, autocommit=True) as admin:
        try:
            # No IF NOT EXISTS and no existing-name reuse: a collision is a hard stop.
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
            )
            created = True
            identity = admin.execute(
                "SELECT oid, datdba FROM pg_database WHERE datname = %s", (database,)
            ).fetchone()
            admin.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(database),
                    sql.Literal(DATABASE_MARKER),
                )
            )
            print("Created isolated test database: " + database, flush=True)
            return run_tests(config, database, sslmode, args.labels)
        finally:
            if created:
                # Long suites can outlive the server's idle connection timeout.
                # Reconnect for cleanup; never depend on the creation connection.
                with psycopg.connect(**connection_args, autocommit=True) as cleanup:
                    actual = cleanup.execute(
                        "SELECT oid, datdba FROM pg_database WHERE datname = %s", (database,)
                    ).fetchone()
                    if identity is None or actual != identity or database == config["dbname"]:
                        raise RuntimeError(
                            "Cleanup identity mismatch; manual review needed: " + database
                        )
                    # Do not FORCE or terminate connections belonging to other processes.
                    cleanup.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
                    print("Removed this run's isolated test database: " + database, flush=True)
            config.clear()


if __name__ == "__main__":
    raise SystemExit(main())
