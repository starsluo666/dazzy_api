"""Requires isolated PostgreSQL; no payment traffic outside synthetic SDK fixtures."""
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from orders import test_wallet_distributions as wallet_fixtures
from providers.test_income_concurrency import parallel
from . import test_pool_recovery as fixtures
from .models import FinanceRecoveryCase as Case, FinanceRecoveryEvent
from .recovery import action
from .watchdog import monitored


@override_settings(**wallet_fixtures.SETTINGS, HUIFU_FINANCE_RECOVERY_ENABLED=True)
class RecoveryConcurrencyTests(TransactionTestCase):
    execute = fixtures.PoolRecoveryTests.execute
    receipt = fixtures.PoolRecoveryTests.receipt
    respond = fixtures.PoolRecoveryTests.respond
    query = fixtures.PoolRecoveryTests.query
    act = fixtures.PoolRecoveryTests.act

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires isolated PostgreSQL row locks.")
        fixtures.PoolRecoveryTests.setUp(self)

    def test_two_reviewers_cannot_release_twice(self):
        revision = self.case.revision
        observation = self.record.observations.order_by("-pk").first()
        proof = {"record_id": self.record.pk, "observation_id": observation.pk}
        def approve():
            try:
                return action(self.case.pk, actor=self.reviewer, revision=revision,
                              action="approve", note="独立核账已确认").status
            except ValidationError:
                return "blocked"
        with patch("financeops.recovery.prove_pool_failure", return_value=proof):
            results = parallel(approve, approve)
        self.assertEqual(sorted(results), ["blocked", "restored"])
        self.assertEqual(FinanceRecoveryEvent.objects.filter(case=self.case, action="approve").count(), 1)
        self.assertEqual(Case.objects.get(pk=self.case.pk).status, "restored")

    def test_job_lease_prevents_parallel_execution(self):
        entered, release = Event(), Event()
        @monitored("providers.process_income_transfers")
        def work():
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("test release timed out")
            return {"done": True}
        def run():
            connections.close_all()
            try:
                return work()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run)
            try:
                self.assertTrue(entered.wait(timeout=15))
                self.assertEqual(work(), {"monitor_busy": True})
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15), {"done": True})
