"""Real row-lock regressions. Run ONLY with the isolated PostgreSQL runner."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from . import test_fulfillment_after_sales as fixtures
from .models import PlatformOperationSetting, ProviderOrderAfterSalesCase as Case
from orders.models import ProviderOrderRefundOrder as Refund


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL with real row locks")
class FulfillmentAfterSalesConcurrencyTests(TransactionTestCase):
    create_fulfillment_order = fixtures.FulfillmentAfterSalesTests.create_fulfillment_order
    make_order = fixtures.FulfillmentAfterSalesTests.make_order
    intake = fixtures.FulfillmentAfterSalesTests.intake
    new_case = fixtures.FulfillmentAfterSalesTests.new_case
    quote = fixtures.FulfillmentAfterSalesTests.quote

    def setUp(self):
        fixtures.FulfillmentAfterSalesTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.FulfillmentAfterSalesTests.setUp(self)

    def race(self, *jobs):
        start = Barrier(len(jobs))

        def run(job):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(self.admin_user)
                start.wait(timeout=10)
                name, ref, body = job
                return client.post(reverse(name, args=(ref,)), body, format="json").status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            return [future.result(timeout=40) for future in futures]

    def decision_job(self, order, case):
        response = self.quote(order, case)
        self.assertEqual(response.status_code, 200, response.data)
        return ("admin-fulfillment-case-resolve", case, {"token": response.data["data"]["token"]})

    def test_duplicate_adjudications_create_one_refund(self):
        order = self.make_order()
        case = self.new_case(order)
        job = self.decision_job(order, case)
        self.assertEqual(self.race(job, job), [200, 200])
        self.assertEqual(Refund.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, "terminated")
        self.assertIsNone(order.confirmation_expires_at)

    def test_transfer_competes_with_normal_review(self):
        order = self.make_order()
        results = self.race(
            ("admin-fulfillment-after-sales", order.order_no, {"revision": 1, "reason": "核实存在问题转售后处理"}),
            ("backoffice-provider-order-fulfillment-review", order.order_no, {"revision": 1, "reason": "核实无误恢复正常履约"}),
        )
        self.assertIn(sorted(results), ([200, 400], [201, 400]))
        order.refresh_from_db()
        if Case.objects.exists():
            self.assertEqual(order.status, "after_sales")
            self.assertTrue(order.fulfillment_review_required)
            self.assertIsNone(order.confirmation_expires_at)
        else:
            self.assertFalse(order.fulfillment_review_required)
        self.assertFalse(Refund.objects.exists())

    def test_reject_competes_with_adjudication(self):
        order = self.make_order()
        case = self.new_case(order)
        results = self.race(self.decision_job(order, case),
            ("backoffice-provider-order-after-sales-action", case, {"action": "reject", "result_note": "继续核查，当前证据不足以裁定"}))
        self.assertEqual(sorted(results), [200, 400])
        order.refresh_from_db()
        if Refund.objects.exists():
            self.assertEqual(order.status, "terminated")
            self.assertFalse(order.fulfillment_review_required)
        else:
            self.assertTrue(order.fulfillment_review_required)
        self.assertIsNone(order.confirmation_expires_at)

    def test_different_orders_share_approver_daily_limit(self):
        setting = PlatformOperationSetting.current()
        setting.support_refund_daily_limit = 5000
        setting.save()
        first, second = self.make_order("RACE-A"), self.make_order("RACE-B")
        one, two = self.new_case(first), self.new_case(second)
        self.assertEqual(self.race(self.decision_job(first, one), self.decision_job(second, two)), [200, 200])
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(Case.objects.filter(requires_supervisor=True).count(), 1)
