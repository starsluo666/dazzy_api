"""Readiness is a local, non-authorizing snapshot. No network or money writes."""
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from backoffice import test_finance_work as fixtures
from orders.test_wallet_distributions import SETTINGS as WALLET_SETTINGS
from .models import PaymentJobHeartbeat


@override_settings(**{
    **WALLET_SETTINGS,
    "HUIFU_PROVIDER_INCOME_JOBS_ENABLED": True,
    "HUIFU_PROVIDER_WITHDRAWAL_ENABLED": True,
    "HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS": 10000,
    "HUIFU_RECONCILIATION_ENABLED": True,
    "HUIFU_RECONCILIATION_START_DATE": "2000-01-01",
    "HUIFU_RECONCILIATION_DOWNLOAD_HOSTS": ("file.cloudpnr.com",),
})
class ReadinessTests(APITestCase):
    setUpTestData = classmethod(fixtures.FinanceWorkTests.setUpTestData.__func__)

    def setUp(self):
        fixtures.FinanceWorkTests.setUp(self)
        self.client.force_authenticate(self.platform_admin)
        self.url = reverse("admin-payment-readiness")

    def report(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.data["data"]

    def groups(self):
        return {group["key"]: group for group in self.report()["groups"]}

    def test_complete_configuration_still_requires_manual_acceptance(self):
        report = self.report()
        self.assertEqual(report["environment"], "正式环境")
        self.assertEqual(len(report["groups"]), 5)
        self.assertTrue(all(group["state"] == "configured" for group in report["groups"]))
        self.assertEqual(len(report["manual_checks"]), 4)
        self.assertNotIn("ready", report)
        self.assertNotIn("approved", report)

    def test_get_does_not_write_or_expose_configuration_values(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        body = response.content.decode()
        for value in (
            settings.HUIFU_RSA_PRIVATE_KEY, settings.HUIFU_RSA_PUBLIC_KEY,
            settings.HUIFU_MERCHANT_ID, settings.HUIFU_SYS_ID, settings.HUIFU_PRODUCT_ID,
            settings.HUIFU_NOTIFY_URL, settings.HUIFU_USER_UPPER_ID,
            settings.HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID,
            settings.HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP,
        ):
            self.assertNotIn(value, body)
        self.assertTrue(settings.HUIFU_PAYMENT_ENABLED)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 405)

    def test_independent_disabled_switches_and_dependency_gaps(self):
        for switch, group in (
            ("HUIFU_PAYMENT_ENABLED", "payment"),
            ("HUIFU_PROVIDER_DISTRIBUTION_ENABLED", "distribution"),
            ("HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED", "balance"),
            ("HUIFU_PROVIDER_WITHDRAWAL_ENABLED", "withdrawal"),
            ("HUIFU_RECONCILIATION_ENABLED", "reconciliation"),
        ):
            with self.subTest(switch=switch), override_settings(**{switch: False}):
                result = self.groups()
                self.assertEqual(result[group]["state"], "disabled")
                if group == "payment":
                    self.assertTrue(all(row["state"] == "incomplete" for key, row in result.items()
                                        if key != "payment"))
        with override_settings(HUIFU_PROVIDER_INCOME_JOBS_ENABLED=False):
            groups = self.groups()
            for key in ("distribution", "balance", "withdrawal"):
                self.assertEqual(groups[key]["state"], "incomplete")
            self.assertEqual(groups["payment"]["state"], "configured")
        with override_settings(HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED=False):
            groups = self.groups()
            self.assertEqual(groups["distribution"]["state"], "incomplete")
            self.assertEqual(groups["balance"]["state"], "configured")

    def test_invalid_balance_policy_is_reported_without_raw_values(self):
        for name, value in (
            ("MAX_CENTS", 0), ("MAX_CENTS", True), ("ACCT_ID", "secret/invalid"),
            ("FEE_MODE", "unknown"), ("FEE_RESERVE_CENTS", -1), ("TRANSFER_TYPE", "99"),
            ("RISK_IP", "127.0.0.1"), ("RISK_IP", "invalid-ip"),
            ("START_AT", "not-a-date"), ("START_AT", "2026-10-21T00:00:00"),
        ):
            key = "HUIFU_PROVIDER_BALANCE_PAYMENT_" + name
            with self.subTest(key=key, value=value), override_settings(**{key: value}):
                row = self.groups()["balance"]
                self.assertEqual(row["state"], "incomplete")
                self.assertFalse(next(check for check in row["checks"] if check["key"] == key)["ok"])

    def test_bad_dates_hosts_and_private_keys_are_incomplete(self):
        with override_settings(HUIFU_RSA_PRIVATE_KEY="SECRET-INVALID-PRIVATE-KEY"):
            response = self.report()
            self.assertEqual(response["groups"][0]["state"], "incomplete")
            self.assertNotIn("SECRET-INVALID-PRIVATE-KEY", str(response))
        future = (timezone.localdate() + timedelta(days=2)).isoformat()
        for changes in (
            {"HUIFU_RECONCILIATION_START_DATE": "invalid"},
            {"HUIFU_RECONCILIATION_START_DATE": future},
            *({"HUIFU_RECONCILIATION_DOWNLOAD_HOSTS": value} for value in (
                (), ("*.cloudpnr.com",), ("https://file.cloudpnr.com",),
                ("127.0.0.1",), ("file..cloudpnr.com",),
            )),
        ):
            with self.subTest(changes=changes), override_settings(**changes):
                self.assertEqual(self.groups()["reconciliation"]["state"], "incomplete")
        with patch("financeops.readiness.validate_transport", side_effect=ValueError("SECRET-ERROR")):
            response = self.report()
            self.assertNotIn("SECRET-ERROR", str(response))
            self.assertEqual(response["groups"][1]["state"], "incomplete")

    def test_view_permission_and_platform_scope_are_both_required(self):
        self.client.force_authenticate(self.admin_user)
        self.role.permissions = ["finance.recovery.view"]
        self.role.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.role.data_scope = "all"
        self.role.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.organization.organization_type = "platform"
        self.organization.save()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.role.permissions = []
        self.role.save()
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_current_task_health_is_returned_without_mutation(self):
        key = "providers.process_income_transfers"
        PaymentJobHeartbeat.objects.filter(key=key).update(
            succeeded_at=timezone.now() - timedelta(hours=2), error_code="", lease_until=None,
        )
        before = list(PaymentJobHeartbeat.objects.values())
        jobs = self.report()["jobs"]
        row = next(item for item in jobs if item["key"] == key)
        self.assertEqual(row["state"], "stale")
        self.assertTrue(row["alert"])
        self.assertEqual(before, list(PaymentJobHeartbeat.objects.values()))
