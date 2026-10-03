"""PostgreSQL row-lock regressions; all channel calls use synthetic fixtures."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
import uuid

from django.db import connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from orders import test_distributions as distribution
from orders.distribution_gateway import HuifuDistributionGateway
from orders.distributions import execute_distribution, query_distribution
from orders.models import ProviderOrder, ProviderOrderDistribution, ProviderOrderRefundOrder
from orders.services import create_provider_order_refund
from . import test_withdrawals as withdrawal
from .models import (
    ProviderIncomeEntry,
    ProviderIncomeWallet,
    ProviderReceivingAccount,
    ProviderWithdrawal,
)
from .withdrawal_gateway import HuifuWithdrawalGateway
from .withdrawals import create_withdrawal, query_withdrawal


def parallel(*functions):
    start = Barrier(len(functions))

    def run(function):
        connections.close_all()
        try:
            start.wait(timeout=15)
            return function()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(run, functions))


@override_settings(**distribution.SETTINGS)
class DistributionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locks and independent connections.")
        distribution.DistributionStateTests.setUp(self)
        guard = patch(
            "requests.sessions.Session.send", side_effect=AssertionError("Real HTTP forbidden")
        )
        guard.start()
        self.addCleanup(guard.stop)

    def refund(self):
        return create_provider_order_refund(
            order_no=self.order.order_no,
            amount=1,
            source_type="admin",
            source_reference="concurrency-test",
            idempotency_key="concurrent-refund",
            reason="synthetic regression",
        )

    def test_concurrent_distribution_dispatches_only_once(self):
        preflight = Barrier(2)

        def verify(snapshot):
            self.assertFalse(connection.in_atomic_block)
            preflight.wait(timeout=15)
            return {
                "payment_fee_amount": 60, "payment_query_digest": "synthetic",
                "platform_split_amount": 3000, "split_amount": 10000,
            }

        def confirm(record):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(ProviderOrderDistribution.objects.filter(pk=record.pk).exists())
            return {"status": "processing"}

        with (
            patch.object(HuifuDistributionGateway, "verify_payment", side_effect=verify),
            patch.object(HuifuDistributionGateway, "confirm", side_effect=confirm) as send,
        ):
            results = parallel(*[lambda: execute_distribution(self.order.order_no)] * 2)
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(len({record.pk for record, _ in results}), 1)
        self.assertEqual(ProviderOrderDistribution.objects.count(), 1)
        send.assert_called_once()

    def test_concurrent_success_queries_credit_once_and_create_one_wallet(self):
        record, _, _ = distribution.DistributionStateTests.run_execute(self)
        self.assertFalse(ProviderIncomeWallet.objects.exists())
        checked = Barrier(2)

        def query(row):
            checked.wait(timeout=15)
            return {"status": "succeeded", "split_fee_amount": 20}

        with patch.object(HuifuDistributionGateway, "query", side_effect=query):
            results = parallel(*[lambda: query_distribution(self.order.order_no)] * 2)
        self.assertEqual([row.status for row in results], ["succeeded", "succeeded"])
        self.assertEqual(ProviderIncomeWallet.objects.count(), 1)
        self.assertEqual(ProviderIncomeWallet.objects.get().available_amount, 7000)
        self.assertEqual(
            ProviderIncomeEntry.objects.filter(distribution=record, kind="credit").count(), 1
        )

    def test_refund_registered_during_distribution_preflight_prevents_dispatch(self):
        in_preflight, refunded = Event(), Event()

        def verify(snapshot):
            in_preflight.set()
            self.assertTrue(refunded.wait(timeout=15))
            return {
                "payment_fee_amount": 60, "payment_query_digest": "synthetic",
                "platform_split_amount": 3000, "split_amount": 10000,
            }

        def split():
            with self.assertRaises(ValidationError):
                execute_distribution(self.order.order_no)

        def refund():
            self.assertTrue(in_preflight.wait(timeout=15))
            try:
                # Synthetic pending-refund fixture, committed on a second
                # connection. Normal refunds are already forbidden after local
                # settlement; dispatch must also reject outstanding records.
                with transaction.atomic():
                    ProviderOrder.objects.select_for_update().get(pk=self.order.pk)
                    return ProviderOrderRefundOrder.objects.create(
                        order=self.order,
                        payment_order=self.payment,
                        beneficiary=self.customer,
                        source_type="admin",
                        source_reference="concurrency-test",
                        idempotency_key="pending-refund-fixture",
                        service_fee_refund_amount=1,
                        transport_fee_refund_amount=0,
                        other_fee_refund_amount=0,
                        refund_amount=1,
                        external_refund_amount=1,
                        reason="synthetic regression",
                    )
            finally:
                refunded.set()

        with (
            patch.object(HuifuDistributionGateway, "verify_payment", side_effect=verify),
            patch.object(
                HuifuDistributionGateway, "confirm", return_value={"status": "processing"}
            ) as send,
        ):
            parallel(split, refund)
        send.assert_not_called()
        self.assertFalse(ProviderOrderDistribution.objects.exists())
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)

    def test_normal_refund_during_preflight_cannot_bypass_settled_ledger_guard(self):
        in_preflight, refund_checked = Event(), Event()

        def verify(snapshot):
            in_preflight.set()
            self.assertTrue(refund_checked.wait(timeout=15))
            return {
                "payment_fee_amount": 60, "payment_query_digest": "synthetic",
                "platform_split_amount": 3000, "split_amount": 10000,
            }

        def refund():
            self.assertTrue(in_preflight.wait(timeout=15))
            try:
                with self.assertRaisesMessage(ValidationError, "已经结算"):
                    self.refund()
            finally:
                refund_checked.set()

        with (
            patch.object(HuifuDistributionGateway, "verify_payment", side_effect=verify),
            patch.object(
                HuifuDistributionGateway, "confirm", return_value={"status": "processing"}
            ) as send,
        ):
            parallel(lambda: execute_distribution(self.order.order_no), refund)
        send.assert_called_once()
        self.assertEqual(ProviderOrderDistribution.objects.get().status, "processing")
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())

    def test_refund_after_committed_distribution_is_rejected_while_channel_pending(self):
        submitting, refund_checked = Event(), Event()

        def confirm(record):
            submitting.set()
            self.assertTrue(refund_checked.wait(timeout=15))
            return {"status": "unknown"}

        def refund():
            self.assertTrue(submitting.wait(timeout=15))
            try:
                with self.assertRaises(ValidationError):
                    self.refund()
            finally:
                refund_checked.set()

        with (
            patch.object(
                HuifuDistributionGateway, "verify_payment",
                return_value={
                    "payment_fee_amount": 60, "platform_split_amount": 3000,
                    "split_amount": 10000, "payment_query_digest": "synthetic",
                },
            ),
            patch.object(HuifuDistributionGateway, "confirm", side_effect=confirm) as send,
        ):
            parallel(lambda: execute_distribution(self.order.order_no), refund)
        send.assert_called_once()
        self.assertEqual(ProviderOrderDistribution.objects.get().status, "unknown")
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())


@override_settings(**withdrawal.SETTINGS)
class WithdrawalConcurrencyTests(TransactionTestCase):
    query_account = withdrawal.WithdrawalStateTests.query_account
    assert_balances = withdrawal.WithdrawalStateTests.assert_balances

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locks and independent connections.")
        withdrawal.WithdrawalStateTests.setUp(self)

    def concurrent_requests(self, keys):
        checked = Barrier(2)

        def refresh(provider):
            # Prove freshness in the fixture, without a real onboarding request.
            ProviderReceivingAccount.objects.filter(provider=provider).update(
                channel_checked_at=timezone.now()
            )

        def balance(receiver):
            checked.wait(timeout=15)
            return {
                "available_amount": 7000,
                "acct_id": "B00000001",
                "response_digest": "synthetic",
            }

        def submit(record, token):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(
                ProviderIncomeWallet.objects.get(pk=self.wallet.pk).reserved_amount, 5000
            )
            return {"status": "processing"}

        def request(key):
            try:
                record, created = create_withdrawal(self.provider, amount=5000, request_key=key)
                return record.pk, created
            except ValidationError:
                return "rejected", False

        with (
            patch("providers.withdrawals.refresh_onboarding", side_effect=refresh),
            patch.object(HuifuWithdrawalGateway, "balance", side_effect=balance),
            patch.object(HuifuWithdrawalGateway, "submit", side_effect=submit) as send,
        ):
            results = parallel(lambda: request(keys[0]), lambda: request(keys[1]))
        send.assert_called_once()
        self.assertEqual(ProviderWithdrawal.objects.count(), 1)
        self.assertEqual(ProviderIncomeEntry.objects.filter(kind="reserve").count(), 1)
        self.assert_balances(2000, 5000, 0)
        return results

    def test_concurrent_same_key_replays_one_reservation_and_one_cash_request(self):
        key = uuid.uuid4()
        results = self.concurrent_requests([key, key])
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(len({pk for pk, _ in results}), 1)
        self.assertNotIn("rejected", [pk for pk, _ in results])

    def test_concurrent_different_keys_cannot_overdraw_or_dispatch_twice(self):
        results = self.concurrent_requests([uuid.uuid4(), uuid.uuid4()])
        self.assertEqual(sum(pk == "rejected" for pk, _ in results), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def reconcile_twice(self, state):
        record, _, _ = withdrawal.WithdrawalStateTests.submit(self)
        checked = Barrier(2)

        def query(row):
            checked.wait(timeout=15)
            return {"status": state, "fee_amount": 10}

        with (
            patch.object(HuifuWithdrawalGateway, "query", side_effect=query),
            patch.object(
                HuifuWithdrawalGateway,
                "balance",
                return_value={"available_amount": 7000, "acct_id": "B00000001"},
            ),
        ):
            results = parallel(*[lambda: query_withdrawal(record)] * 2)
        self.assertEqual([row.status for row in results], [state, state])
        self.assertEqual(
            ProviderIncomeEntry.objects.filter(
                source_key=f"withdrawal:{record.pk}:terminal"
            ).count(),
            1,
        )

    def test_concurrent_success_queries_debit_reserved_funds_only_once(self):
        self.reconcile_twice("succeeded")
        self.assert_balances(5000, 0, 2000)

    def test_concurrent_failure_queries_release_reserved_funds_only_once(self):
        self.reconcile_twice("failed")
        self.assert_balances(7000, 0, 0)
