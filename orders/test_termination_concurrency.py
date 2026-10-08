"""Run on an isolated PostgreSQL database; SQLite is not proof of row locking."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import connection, connections, close_old_connections
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from backoffice.models import ProviderOrderAfterSalesCase as Case
from . import test_termination as fixtures


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class TerminationConcurrencyTests(TransactionTestCase):
    create_fulfillment_order = fixtures.TerminationTests.create_fulfillment_order
    submit = fixtures.TerminationTests.submit
    decision = fixtures.TerminationTests.decision
    complete_refund = fixtures.TerminationTests.complete_refund

    def setUp(self):
        fixtures.TerminationTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.TerminationTests.setUp(self)

    def race(self, *jobs):
        barrier = Barrier(len(jobs))

        def run(job):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return job()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            return [future.result(timeout=30) for future in futures]

    def request(self, actor, prefix):
        client = APIClient()
        client.force_authenticate(actor)
        return client.post(f"/api/v1/{prefix}/{self.order.order_no}/termination/", {
            "ended_at": self.ended_at.isoformat(), "reason": "并发提交服务提前终止申请",
        }, format="json").status_code

    def review(self, action):
        client = APIClient()
        client.force_authenticate(self.admin_user)
        return client.post(reverse("backoffice-provider-order-after-sales-action", args=(Case.objects.get(order=self.order).case_no,)), {
            "action": action, "ended_at": self.ended_at.isoformat(), "responsibility": "provider",
            "component_refunds": {"service": 7500, "transport": 900, "other": 0},
            "result_note": "并发审核测试核定部分退款",
        }, format="json").status_code

    def test_customer_provider_simultaneous_requests_create_one_case(self):
        results = self.race(lambda: self.request(self.order.customer, "provider-orders"),
                            lambda: self.request(self.order.provider.user, "providers/me/orders"))
        self.assertEqual(sorted(results), [200, 201])
        self.assertEqual(Case.objects.filter(order=self.order).count(), 1)

    def test_duplicate_decision_creates_one_refund(self):
        self.submit()
        results = self.race(lambda: self.review("resolve_termination"), lambda: self.review("resolve_termination"))
        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(self.order.refund_orders.count(), 1)

    def test_approve_reject_race_has_one_terminal_outcome(self):
        self.submit()
        self.assertEqual(sorted(self.race(lambda: self.review("resolve_termination"), lambda: self.review("reject"))), [200, 400])
        case = Case.objects.get(order=self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated" if case.status == "approved" else "in_service")
        self.assertEqual(self.order.refund_orders.count(), 1 if case.status == "approved" else 0)

    def test_duplicate_success_callbacks_do_not_restore_service(self):
        self.submit()
        self.decision()
        results = self.race(lambda: self.complete_refund()[1], lambda: self.complete_refund()[1])
        self.assertEqual(sorted(results), [False, True])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated")
        self.assertEqual(self.order.settlement.refunded_amount, 8400)
        self.assertEqual(self.order.settlement.status, "dispute_frozen")
