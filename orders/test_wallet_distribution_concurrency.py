"""PostgreSQL-only lock regressions; no production data or network."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from providers.models import ProviderIncomeEntry
from providers.test_income_concurrency import parallel
from . import test_wallet_distributions as fixtures
from .balance_payment_gateway import HuifuBalancePaymentGateway
from .distributions import query_distribution
from .models import ProviderOrderDistribution
from .wallet_distributions import execute_wallet_distribution


@override_settings(**fixtures.SETTINGS)
class WalletDistributionConcurrencyTests(TransactionTestCase):
    execute = fixtures.WalletDistributionTests.execute
    respond = fixtures.WalletDistributionTests.respond
    receipt = fixtures.WalletDistributionTests.receipt
    second_order = fixtures.WalletDistributionTests.second_order
    refund = fixtures.WalletDistributionTests.refund

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locks and independent connections.")
        fixtures.WalletDistributionTests.setUp(self)

    def test_same_order_concurrent_execution_submits_once(self):
        barrier = Barrier(2)
        def source(_, **kwargs):
            barrier.wait(timeout=15)
            return "synthetic"
        with patch.object(HuifuBalancePaymentGateway, "verify_source", side_effect=source), patch.object(
            HuifuBalancePaymentGateway, "platform_balance", return_value={"available_amount": 100000, "response_digest": "synthetic"},
        ), patch.object(HuifuBalancePaymentGateway, "pay", return_value={"status": "processing"}) as pay:
            results = parallel(*[lambda: execute_wallet_distribution(self.order.order_no) for _ in range(2)])
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(ProviderOrderDistribution.objects.count(), 1)
        self.assertEqual(pay.call_count, 1)

    def test_two_orders_cannot_spend_same_account_balance_concurrently(self):
        other = self.second_order()
        barrier = Barrier(2)
        def source(_, **kwargs):
            barrier.wait(timeout=15)
            return "synthetic"
        def run(order_no):
            try:
                return execute_wallet_distribution(order_no)[1]
            except ValidationError:
                return False
        with patch.object(HuifuBalancePaymentGateway, "verify_source", side_effect=source), patch.object(
            HuifuBalancePaymentGateway, "platform_balance", return_value={"available_amount": 7100, "response_digest": "synthetic"},
        ), patch.object(HuifuBalancePaymentGateway, "pay", return_value={"status": "unknown"}) as pay:
            results = parallel(lambda: run(self.order.order_no), lambda: run(other.order_no))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(pay.call_count, 1)

    def test_two_success_queries_credit_and_release_only_once(self):
        record, _, _ = self.execute()
        barrier = Barrier(2)
        def query(*_, **kwargs):
            barrier.wait(timeout=15)
            return {"status": "succeeded", "channel_fee_amount": 12, "gateway_trade_no": "OFFLINE-BALANCE"}
        with patch.object(HuifuBalancePaymentGateway, "query", side_effect=query):
            parallel(*[lambda: query_distribution(self.order.order_no) for _ in range(2)])
        record.refresh_from_db()
        self.assertEqual(ProviderIncomeEntry.objects.count(), 1)
        self.assertTrue(record.funds_released)
        self.assertEqual(record.funding_pool.generation, 2)

    def test_concurrent_refund_wins_before_durable_registration(self):
        entered, release = Event(), Event()
        def source(_, **kwargs):
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("preflight release timed out")
            return "synthetic"
        def run():
            connections.close_all()
            try:
                return execute_wallet_distribution(self.order.order_no)
            finally:
                connections.close_all()
        with patch.object(HuifuBalancePaymentGateway, "verify_source", side_effect=source), patch.object(
            HuifuBalancePaymentGateway, "platform_balance", return_value={"available_amount": 100000, "response_digest": "synthetic"},
        ), patch.object(HuifuBalancePaymentGateway, "pay") as pay, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run)
            try:
                self.assertTrue(entered.wait(timeout=15))
                self.refund()
            finally:
                release.set()
            with self.assertRaises(ValidationError):
                future.result(timeout=15)
        pay.assert_not_called()
        self.assertFalse(ProviderOrderDistribution.objects.exists())
