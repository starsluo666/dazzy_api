from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch
import uuid

from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import PaymentJobHeartbeat as Heartbeat
from .watchdog import JOBS, bill_coverage, health, monitored

KEY = "providers.process_income_transfers"


@override_settings(HUIFU_PROVIDER_INCOME_JOBS_ENABLED=True, HUIFU_RECONCILIATION_ENABLED=False)
class WatchdogTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        for key in JOBS:
            Heartbeat.objects.update_or_create(key=key, defaults={"succeeded_at": self.now})

    def row(self):
        return Heartbeat.objects.get(key=KEY)

    def state(self):
        return next(item for item in health(self.now) if item["key"] == KEY)

    def test_read_health_never_writes_or_calls_channel(self):
        before = list(Heartbeat.objects.values())
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No network")):
            self.assertEqual(self.state()["state"], "healthy")
            call_command("check_payment_jobs", stdout=StringIO())
        self.assertEqual(before, list(Heartbeat.objects.values()))

    def test_missing_stale_stuck_disabled_and_failed(self):
        Heartbeat.objects.filter(key=KEY).update(succeeded_at=self.now - timedelta(hours=2))
        self.assertEqual(self.state()["state"], "stale")
        with self.assertRaises(CommandError):
            call_command("check_payment_jobs", stdout=StringIO())
        Heartbeat.objects.filter(key=KEY).update(lease_until=self.now - timedelta(seconds=1))
        self.assertEqual(self.state()["state"], "stuck")
        with override_settings(HUIFU_PROVIDER_INCOME_JOBS_ENABLED=False):
            self.assertEqual(self.state()["state"], "disabled")
            self.assertFalse(self.state()["alert"])
        Heartbeat.objects.filter(key=KEY).delete()
        self.assertEqual(self.state()["state"], "missing")

    def test_success_and_exceptions_never_store_exception_payload(self):
        old = self.row().succeeded_at
        @monitored(KEY)
        def broken():
            raise ValueError("SECRET-DO-NOT-PERSIST")
        with self.assertRaises(ValueError):
            broken()
        self.assertEqual(self.row().error_code, "task_failed")
        self.assertEqual(self.row().succeeded_at, old)
        self.assertNotIn("SECRET", str(list(Heartbeat.objects.values())))
        self.assertEqual(self.state()["state"], "failed")
        self.assertEqual(monitored(KEY)(lambda: {"ok": True})(), {"ok": True})
        self.assertEqual(self.row().error_code, "")
        self.assertIsNone(self.row().lease_until)

    def test_disabled_result_is_not_a_success(self):
        old = self.row().succeeded_at
        monitored(KEY)(lambda: {"disabled": True})()
        self.assertEqual(self.row().succeeded_at, old)
        self.assertEqual(self.row().error_code, "task_disabled")

    def test_concurrent_invocation_skips_and_old_worker_cannot_overwrite_new_run(self):
        Heartbeat.objects.filter(key=KEY).update(lease_until=self.now + timedelta(minutes=10))
        callback = monitored(KEY)(lambda: self.fail("Duplicate job"))
        self.assertTrue(callback()["monitor_busy"])
        Heartbeat.objects.filter(key=KEY).update(lease_until=None)
        token = uuid.uuid4()
        def replaced():
            Heartbeat.objects.filter(key=KEY).update(run_token=token, error_code="task_failed")
        monitored(KEY)(replaced)()
        self.assertEqual(self.row().run_token, token)
        self.assertEqual(self.row().error_code, "task_failed")

    @override_settings(HUIFU_RECONCILIATION_ENABLED=True)
    def test_bill_coverage_is_not_hidden_by_healthy_scheduler(self):
        with patch("financeops.watchdog.bill_coverage", return_value="缺少应到账单"):
            row = next(item for item in health(self.now) if item["key"] == "reconciliation.daily_bills")
            Heartbeat.objects.filter(key="reconciliation.daily_bills").update(succeeded_at=self.now + timedelta(seconds=1))
            again = next(item for item in health(self.now) if item["key"] == "reconciliation.daily_bills")
        self.assertTrue(row["alert"])
        self.assertEqual(row["state"], "coverage")
        self.assertEqual(row["event_version"], again["event_version"])

    @override_settings(HUIFU_RECONCILIATION_START_DATE="2026-10-19")
    def test_due_bill_coverage_requires_all_types_and_has_window_grace(self):
        from reconciliation.models import BILL_TYPES, ReconciliationBatch
        # Clock is UTC: before 20:30 China time yesterday is not yet overdue.
        with patch("orders.huifu.HuifuPaymentConfig.from_settings"), patch("orders.distributions._scope", return_value="offline"):
            self.assertEqual(bill_coverage(datetime(2026, 10, 20, 12, 0, tzinfo=datetime_timezone.utc)), "")
            now = datetime(2026, 10, 20, 13, 0, tzinfo=datetime_timezone.utc)
            self.assertIn("2026-10-19", bill_coverage(now))
            for kind, _ in BILL_TYPES:
                ReconciliationBatch.objects.create(scope="offline", data_date="2026-10-19", bill_type=kind, status="matched")
            self.assertEqual(bill_coverage(now), "")
            ReconciliationBatch.objects.filter(bill_type=BILL_TYPES[0][0]).update(status="waiting")
            self.assertIn("2026-10-19", bill_coverage(now))
