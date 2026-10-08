"""Run only on a dedicated PostgreSQL test database; never simulates row locks."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import connection, connections, close_old_connections
from django.test import TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from activities.models import ActivityParticipationRefundOrder
from orders.models import ProviderOrderRefundOrder
from .models import ProviderOrderAfterSalesCase
from . import test_staff_refunds as fixtures


@skipUnless(
    connection.vendor == "postgresql",
    "Requires a dedicated PostgreSQL test database with row locks",
)
class StaffRefundConcurrencyTests(TransactionTestCase):
    create_fulfillment_order = fixtures.StaffRefundTests.create_fulfillment_order
    limits = fixtures.StaffRefundTests.limits
    order = fixtures.StaffRefundTests.order
    register = fixtures.StaffRefundTests.register
    register_activity = fixtures.StaffRefundTests.register_activity
    new_case = fixtures.StaffRefundTests.new_case

    def setUp(self):
        fixtures.StaffRefundTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.StaffRefundTests.setUp(self)
        self.limits(single=10000, daily=1500)

    def race(self, *jobs):
        start = Barrier(len(jobs))

        def run(job):
            close_old_connections()
            try:
                start.wait(timeout=10)
                client = APIClient()
                client.force_authenticate(self.admin_user)
                kind, case = job
                name = (
                    "backoffice-provider-order-after-sales-action"
                    if kind == "provider"
                    else "backoffice-activity-after-sales-action"
                )
                body = {"action": "approve", "result_note": "并发测试审批核实通过"}
                body.update(
                    {"approved_amount": 1000}
                    if kind == "provider"
                    else {"approved_principal_amount": 1000, "approved_service_fee_amount": 0}
                )
                return client.post(reverse(name, args=(case,)), body, format="json").status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            return [future.result(timeout=30) for future in futures]

    def test_same_case_creates_only_one_refund(self):
        case = self.new_case()
        self.assertEqual(sorted(self.race(("provider", case), ("provider", case))), [200, 400])
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)

    def test_two_orders_share_one_approvers_daily_lock(self):
        first = self.new_case(self.order("CONCURRENT-A"))
        second = self.new_case(self.order("CONCURRENT-B"))
        self.assertEqual(self.race(("provider", first), ("provider", second)), [200, 200])
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)
        self.assertEqual(
            ProviderOrderAfterSalesCase.objects.filter(requires_supervisor=True).count(), 1
        )

    def test_activity_and_provider_share_one_approvers_daily_lock(self):
        provider = self.new_case()
        response = self.register_activity()
        self.assertEqual(response.status_code, 201, response.data)
        activity = response.data["data"]["case_no"]
        self.assertEqual(self.race(("provider", provider), ("activity", activity)), [200, 200])
        self.assertEqual(
            ProviderOrderRefundOrder.objects.count()
            + ActivityParticipationRefundOrder.objects.count(),
            1,
        )
