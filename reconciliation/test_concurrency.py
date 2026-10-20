"""Requires an isolated PostgreSQL test database, never production credentials."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from . import tests as fixtures
from .models import ReconciliationBatch as Batch
from .services import run_batch
from orders.distributions import _scope
from orders.huifu import HuifuPaymentConfig


@override_settings(**fixtures.OPTIONS)
class ReconciliationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locks")

    def test_two_workers_download_only_once(self):
        config = HuifuPaymentConfig.from_settings()
        batch = Batch.objects.create(scope=_scope(config), data_date=fixtures.DAY, bill_type="TRADE_BILL")
        entered, release = Event(), Event()

        def download(*args):
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("Test barrier timeout")
            return [], "a" * 64

        def worker():
            connections.close_all()
            try:
                return run_batch(batch.pk, config=config)
            finally:
                connections.close_all()

        with patch("reconciliation.services.timezone.now", return_value=fixtures.NOW), \
                patch("reconciliation.services.fetch_bill", side_effect=download) as fetch, \
                patch("reconciliation.services.records_for", return_value=[]), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(timeout=15))
                self.assertFalse(pool.submit(worker).result(timeout=15))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=15))
            self.assertEqual(fetch.call_count, 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "matched")
        self.assertEqual(batch.attempts, 1)
