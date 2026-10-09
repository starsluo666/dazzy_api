"""PostgreSQL-only regressions. Run in a disposable DB, never against live data."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError

from . import cancellations as c, test_cancellations as fixtures


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks')
class CancellationConcurrencyTests(TransactionTestCase):
    create_fulfillment_order = fixtures.CancellationTests.create_fulfillment_order
    depart = fixtures.CancellationTests.depart
    waiting = fixtures.CancellationTests.waiting

    def setUp(self):
        fixtures.CancellationTests.setUpTestData.__func__(type(self))
        self.client = APIClient()
        fixtures.CancellationTests.setUp(self)

    def race(self, *jobs):
        barrier = Barrier(len(jobs))
        def run(job):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return job()
                except ValidationError:
                    return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = [executor.submit(run, job) for job in jobs]
            return [future.result(timeout=30) for future in futures]

    def test_simultaneous_same_confirmation_only_one_refund(self):
        token = c.preview(order_no=self.order.order_no, customer=self.order.customer)['token']
        def submit():
            return c.cancel(order_no=self.order.order_no, customer=self.order.customer,
                token=token, personal_reason_confirmed=True).status
        self.assertEqual(self.race(submit, submit), ['cancelled', 'cancelled'])
        self.assertEqual(self.order.refund_orders.count(), 1)

    def test_customer_cancellation_racing_no_show_never_stacks(self):
        order = self.waiting()
        token = c.preview(order_no=order.order_no, customer=order.customer)['token']
        self.race(lambda: c.cancel(order_no=order.order_no, customer=order.customer, token=token, personal_reason_confirmed=True),
                  lambda: c.expire_wait(order.order_no, now=order.customer_wait_deadline_at))
        self.assertEqual(order.refund_orders.count(), 1)
        self.assertIn(order.refund_orders.get().refund_amount, (10000, 10500))

    def test_user_response_racing_no_show_has_one_terminal_outcome(self):
        order = self.waiting()
        self.race(lambda: c.respond_wait(order_no=order.order_no, actor=order.customer, role='customer'),
                  lambda: c.expire_wait(order.order_no, now=order.customer_wait_deadline_at))
        order.refresh_from_db()
        if order.customer_wait['state'] == 'responded':
            self.assertFalse(order.refund_orders.exists())
        else:
            self.assertEqual(order.status, 'cancelled')
            self.assertEqual(order.refund_orders.count(), 1)
