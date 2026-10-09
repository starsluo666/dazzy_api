from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APITestCase

from backoffice import tests as fixtures
from backoffice.models import PlatformOperationSetting
from . import cancellations as c
from .models import ProviderOrder, ProviderOrderSettlement
from .services import _complete_provider_order_refund, advance_provider_order_settlement
from . import tests as order_fixtures


class CancellationTests(APITestCase):
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    @classmethod
    def setUpTestData(cls):
        fixtures.BackofficeProviderReviewTests.setUpTestData.__func__(cls)

    def setUp(self):
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("No live channel calls"))
        guard.start()
        self.addCleanup(guard.stop)
        setting = PlatformOperationSetting.current()
        setting.provider_cancellation_enabled = True
        setting.save()
        self.order = self.create_fulfillment_order(order_no="CANCEL-1", provider=self.handan, status="pending_service")
        self.order.pricing_snapshot = {"platform_commission_rate": "30"}
        self.order.service_fee_amount, self.order.transport_fee_amount = 15000, 1800
        self.order.cancellation_policy = c.current_policy()
        self.order.transport_mode = "bus"
        self.order.departed_at = self.order.service_started_at = None
        self.order.save()
        self.url = f"/api/v1/provider-orders/{self.order.order_no}/cancellation/"
        self.client.force_authenticate(self.order.customer)

    def depart(self, *, minutes=10, mode="bus"):
        self.order.status = "departed"
        self.order.transport_mode = mode
        self.order.departed_at = timezone.now() - timedelta(minutes=minutes)
        self.order.save()

    def confirm(self, token=None, **data):
        if token is None:
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200, response.data)
            token = response.data["data"]["token"]
        return self.client.post(self.url, {"token": token, "personal_reason_confirmed": True, **data}, format="json")

    def complete(self):
        refund = self.order.refund_orders.get()
        return _complete_provider_order_refund(refund.refund_no, gateway_refund_no="OFFLINE", refunded_at=timezone.now())

    def test_before_departure_full_refund_and_repeated_confirm(self):
        token = self.client.get(self.url).data["data"]["token"]
        self.assertEqual(self.confirm(token).status_code, 200)
        self.assertEqual(self.confirm(token).status_code, 200)
        self.assertEqual(self.order.refund_orders.count(), 1)
        self.assertEqual(self.order.refund_orders.get().refund_amount, 16800)
        self.complete()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(self.order.settlement.status, "cancelled")
        self.assertFalse(self.complete()[1])

    def test_all_mode_boundaries_and_no_stacking(self):
        now = timezone.now()
        for mode in c.MODES:
            for seconds in (1199, 1200, 1201):
                self.order.departed_at = now - timedelta(seconds=seconds)
                self.order.transport_mode = mode
                value = c.calculate(self.order, now=now)
                expected = (3000 if seconds <= 1200 else 8000) if mode in ("bus", "subway") else 1800
                self.assertEqual(value["retained_amount"], expected)
                self.order.arrived_at = now
                self.assertEqual(c.calculate(self.order, now=now)["retained_amount"], 6300)
                self.order.arrived_at = None

    def test_transit_compensation_split_and_original_rate(self):
        self.depart()
        response = self.confirm()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.order.refund_orders.get().refund_amount, 13800)
        self.order.pricing_snapshot = {"platform_commission_rate": "90"}
        self.order.save(update_fields=("pricing_snapshot",))
        self.complete()
        settlement = ProviderOrderSettlement.objects.get(order=self.order)
        self.assertEqual(settlement.platform_commission_amount, 900)
        self.assertEqual(settlement.provider_settlement_amount, 2100)
        self.assertEqual(settlement.net_transport_fee_amount, 0)
        self.assertEqual(settlement.status, "dispute_frozen")
        self.assertIn("cancellation_reconciliation", [x["code"] for x in settlement.distribution_plan.blockers])
        self.assertEqual(advance_provider_order_settlement(order_no=self.order.order_no)["state"], "cancellation_reconciliation")

    def test_compensation_uses_transport_funding_but_not_transport_commission(self):
        self.depart()
        self.order.service_fee_amount, self.order.transport_fee_amount = 1000, 15800
        self.order.save()
        self.assertEqual(self.confirm().status_code, 200)
        self.complete()
        settlement = ProviderOrderSettlement.objects.get(order=self.order)
        self.assertEqual(settlement.net_service_fee_amount, 3000)
        self.assertEqual(settlement.net_transport_fee_amount, 0)
        self.assertEqual(settlement.platform_commission_amount, 900)

    def test_arrival_service_no_show_and_coupons(self):
        self.depart()
        self.order.arrived_at = timezone.now()
        self.assertEqual(c.calculate(self.order, now=timezone.now())["refund_amount"], 10500)
        self.assertEqual(c.calculate(self.order, now=timezone.now(), no_show=True)["retained_amount"], 6800)
        self.order.discount_amount, self.order.payable_amount = 1000, 15800
        self.assertEqual(c.calculate(self.order, now=timezone.now())["compensation_amount"], 4200)
        self.order.service_started_at = timezone.now()
        self.assertEqual(c.calculate(self.order, now=timezone.now())["refund_amount"], 0)

    def test_cap_never_negative_or_extra_debit(self):
        self.depart(minutes=30)
        self.order.service_fee_amount, self.order.transport_fee_amount, self.order.payable_amount = 1000, 200, 1200
        amount = c.calculate(self.order, now=timezone.now())
        self.assertEqual(amount["refund_amount"], 0)
        self.assertEqual(amount["compensation_amount"], 1200)
        self.assertTrue(all(x >= 0 for x in amount["component_refunds"].values()))

    def test_stale_phase_and_threshold_require_reconfirmation(self):
        token = self.client.get(self.url).data["data"]["token"]
        self.depart()
        self.assertEqual(self.confirm(token).status_code, 400)
        token = self.client.get(self.url).data["data"]["token"]
        with patch("orders.cancellations.timezone.now", return_value=timezone.now() + timedelta(minutes=15)):
            self.assertEqual(self.confirm(token).status_code, 400)
        self.assertFalse(self.order.refund_orders.exists())

    def test_permission_signature_and_explicit_consent(self):
        self.assertEqual(self.confirm({"invalid": True}).status_code, 400)
        self.assertEqual(self.confirm("invalid").status_code, 400)
        self.assertEqual(self.confirm(personal_reason_confirmed=False).status_code, 400)
        self.client.force_authenticate(self.admin_user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_legacy_hold_and_prior_refund_fail_closed(self):
        self.order.cancellation_policy = {}
        self.order.save()
        self.assertEqual(self.client.get(self.url).status_code, 400)
        self.order.cancellation_policy = c.current_policy()
        self.order.fulfillment_review_required = True
        self.order.save()
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_zero_refund_in_service_is_terminal_but_not_spendable(self):
        self.order.status = "in_service"
        self.order.service_started_at = timezone.now()
        self.order.save()
        self.assertEqual(self.confirm().status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated")
        self.assertFalse(self.order.refund_orders.exists())
        self.assertEqual(self.order.settlement.status, "dispute_frozen")

    def waiting(self):
        self.depart()
        now = timezone.now()
        self.order.arrived_at = now
        self.order.provider_last_contact_at = now
        self.order.start_deadline_at = now + timedelta(hours=1)
        self.order.save()
        return c.start_wait(order_no=self.order.order_no, provider_user=self.order.provider.user, confirmed=True)

    def test_wait_requires_arrival_and_new_contact(self):
        from rest_framework.exceptions import ValidationError
        self.depart()
        with self.assertRaises(ValidationError):
            c.start_wait(order_no=self.order.order_no, provider_user=self.order.provider.user, confirmed=True)
        self.order.arrived_at = timezone.now()
        self.order.provider_last_contact_at = self.order.arrived_at - timedelta(seconds=1)
        self.order.save()
        with self.assertRaises(ValidationError):
            c.start_wait(order_no=self.order.order_no, provider_user=self.order.provider.user, confirmed=True)

    def test_wait_deadline_idempotency_and_response_prevents_charge(self):
        order = self.waiting()
        again = c.start_wait(order_no=order.order_no, provider_user=order.provider.user, confirmed=True)
        self.assertEqual(order.customer_wait_deadline_at, again.customer_wait_deadline_at)
        self.assertGreaterEqual(order.customer_wait_deadline_at, order.starts_at + timedelta(minutes=20))
        self.assertEqual(c.expire_wait(order.order_no)["state"], "not_due")
        c.respond_wait(order_no=order.order_no, actor=order.customer, role="customer")
        self.assertEqual(c.expire_wait(order.order_no, now=order.customer_wait_deadline_at)["state"], "not_applicable")
        self.assertFalse(order.refund_orders.exists())

    def test_wait_expiry_uses_no_show_not_arrived_penalty(self):
        order = self.waiting()
        self.assertEqual(c.expire_wait(order.order_no, now=order.customer_wait_deadline_at)["state"], "cancelled")
        self.assertEqual(order.refund_orders.get().refund_amount, 10000)
        self.assertEqual(c.expire_wait(order.order_no, now=order.customer_wait_deadline_at)["state"], "not_applicable")

    def test_wait_dispute_routes_to_review_not_charge(self):
        order = self.waiting()
        order.fulfillment_review_required = True
        order.save()
        self.assertEqual(c.expire_wait(order.order_no, now=order.customer_wait_deadline_at)["state"], "review")
        self.assertFalse(order.refund_orders.exists())

    def test_after_sales_permanently_interrupts_wait_before_restoration(self):
        from .services import create_customer_provider_order_after_sales_case
        from .timeouts import inspect_idle_fulfillment
        from rest_framework.exceptions import ValidationError
        order = self.waiting()
        case, created = create_customer_provider_order_after_sales_case(
            order_no=order.order_no, customer=order.customer, case_type="refund",
            requested_amount=order.payable_amount, reason="到场记录存在争议",
        )
        self.assertTrue(created)
        order.refresh_from_db()
        self.assertEqual(order.customer_wait["state"], "review")
        self.assertEqual(order.customer_wait["case_no"], case.case_no)
        self.assertEqual(c.expire_wait(order.order_no, now=order.customer_wait_deadline_at)["state"], "not_applicable")
        # Even if support restores the original phase, no-show charging cannot resume.
        case.status = "rejected"
        case.save()
        order.status = case.original_order_status
        order.departure_deadline_at = order.starts_at
        order.save()
        self.assertEqual(c.expire_wait(order.order_no, now=order.customer_wait_deadline_at)["state"], "not_applicable")
        with self.assertRaises(ValidationError):
            c.start_wait(order_no=order.order_no, provider_user=order.provider.user, confirmed=True)
        result = inspect_idle_fulfillment(order.order_no, stage="start", now=order.start_deadline_at + timedelta(seconds=1))
        self.assertEqual(result["state"], "processed")
        self.assertFalse(order.refund_orders.exists())

    def test_stale_wait_on_inactive_order_does_not_reschedule_watchdog_forever(self):
        from .timeouts import inspect_idle_fulfillment
        order = self.waiting()
        order.departure_deadline_at = order.starts_at
        for status in ("after_sales", "cancelled", "refunded", "in_service"):
            order.status = status
            order.save()
            self.assertEqual(inspect_idle_fulfillment(order.order_no, stage="start")["state"], "not_applicable")

    def test_mismatched_payment_owner_fails_closed(self):
        from .models import ProviderOrderPaymentOrder
        ProviderOrderPaymentOrder.objects.filter(order=self.order).update(payer=self.admin_user)
        self.assertEqual(self.client.get(self.url).status_code, 400)
        self.assertFalse(self.order.refund_orders.exists())

    def test_policy_hash_changes_and_old_snapshot_stays(self):
        old = c.current_policy()
        setting = PlatformOperationSetting.current()
        setting.provider_cancellation_config = {"no_show_amount": 6000}
        setting.save()
        self.assertNotEqual(c.current_policy()["version"], old["version"])
        self.assertEqual(self.order.cancellation_policy, old)
        from rest_framework.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            c.booking_policy({"transport_mode": "bus", "cancellation_policy_version": old["version"]})

    def test_config_rejects_invalid_values(self):
        from rest_framework.exceptions import ValidationError
        for value in ([], {"unknown": 1}, {"wait_minutes": 0}, {"no_show_amount": True},
                      {"transit_early_amount": 9000, "transit_late_amount": 1000}, {"arrived_penalty_percent": 101}):
            with self.assertRaises(ValidationError):
                c.validate_config(value)

    def test_arrival_distance_accuracy_and_original_evidence(self):
        from rest_framework.exceptions import ValidationError
        from types import SimpleNamespace
        self.depart()
        now = timezone.now()
        self.order.source_longitude, self.order.source_latitude = 114.5, 36.6
        data = {"longitude": 114.5, "latitude": 36.6, "accuracy_m": 10}
        photo = SimpleNamespace(pk="new-photo", created_at=now)
        with self.assertRaises(ValidationError):
            c.confirm_arrival(self.order, {**data, "longitude": 115}, photo, now=now)
        with self.assertRaises(ValidationError):
            c.confirm_arrival(self.order, {**data, "accuracy_m": None}, photo, now=now)
        with self.assertRaises(ValidationError):
            c.confirm_arrival(self.order, data, SimpleNamespace(pk="old", created_at=now-timedelta(hours=1)), now=now)
        c.confirm_arrival(self.order, data, photo, now=now)
        c.confirm_arrival(self.order, data, photo, now=now+timedelta(minutes=10))
        self.assertEqual(self.order.arrived_at, now)
        with self.assertRaises(ValidationError):
            c.confirm_arrival(self.order, data, SimpleNamespace(pk="replacement", created_at=now), now=now)

    def test_wait_task_recovery_and_handler(self):
        from taskcenter.models import ScheduledTask
        from taskcenter.services import synchronize_provider_fulfillment_timeouts, TASK_HANDLERS
        order = self.waiting()
        ScheduledTask.objects.filter(task_type="provider_customer_wait", business_key=order.order_no).delete()
        self.assertEqual(synchronize_provider_fulfillment_timeouts(batch_size=10), 1)
        task = ScheduledTask.objects.get(task_type="provider_customer_wait", business_key=order.order_no)
        result = TASK_HANDLERS[task.task_type](task, order.customer_wait_deadline_at)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.result["state"], "cancelled")

    def test_queue_scoped_and_visible_before_refund_finishes(self):
        from backoffice.access import resolve_admin_access
        from backoffice.operations_queue import queues
        self.depart()
        self.assertEqual(self.confirm().status_code, 200)
        self.role.permissions = ["order.finance.view", "order.fulfillment.view"]
        self.role.save()
        items = {q.key: q for q in queues(resolve_admin_access(self.admin_user))}
        self.assertIn("cancellation_reconciliation", items)
        self.assertEqual(list(items["cancellation_reconciliation"].queryset.values_list("order_no", flat=True)), [self.order.order_no])
        self.complete()
        self.assertEqual(items["cancellation_reconciliation"].queryset.count(), 1)

    def test_refund_does_not_change_status_or_create_spendable_income(self):
        self.depart(mode="taxi")
        self.assertEqual(self.confirm().status_code, 200)
        self.complete()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(self.order.settlement.provider_settlement_amount, 1800)
        self.assertEqual(self.order.settlement.platform_commission_amount, 0)
        from backoffice.refund_views import refund_context
        self.assertIn("取消规则", refund_context(self.order, "provider")["blocked_reason"])


class CancellationBookingTests(TestCase):
    setUp = order_fixtures.ProviderOrderApiTests.setUp
    payload = order_fixtures.ProviderOrderApiTests.payload

    def test_preview_creation_requires_matching_explicit_consent(self):
        setting = PlatformOperationSetting.current()
        setting.provider_cancellation_enabled = True
        setting.save()
        result = self.client.post('/api/v1/provider-orders/preview/', self.payload(), content_type='application/json')
        self.assertEqual(result.status_code, 200, result.content)
        policy = result.json()['data']['cancellation_policy']
        response = self.client.post('/api/v1/provider-orders/', self.payload(), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        request = {**self.payload(), 'cancellation_policy_version': policy['version'], 'transport_mode': 'bus'}
        response = self.client.post('/api/v1/provider-orders/', request, content_type='application/json')
        self.assertEqual(response.status_code, 201, response.content)
        order = ProviderOrder.objects.get(order_no=response.json()['data']['order_no'])
        self.assertEqual(order.cancellation_policy['version'], policy['version'])
        self.assertTrue(order.cancellation_policy['agreed_at'])
        self.assertEqual(order.transport_mode, 'bus')

    def test_policy_disabled_after_preview_requires_new_consent(self):
        setting = PlatformOperationSetting.current()
        setting.provider_cancellation_enabled = True
        setting.save()
        policy = c.current_policy()
        setting.provider_cancellation_enabled = False
        setting.save()
        response = self.client.post('/api/v1/provider-orders/', {
            **self.payload(), 'cancellation_policy_version': policy['version'], 'transport_mode': 'taxi',
        }, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProviderOrder.objects.exists())

    def test_feature_off_leaves_legacy_orders_unenrolled(self):
        result = self.client.post('/api/v1/provider-orders/', self.payload(), content_type='application/json')
        self.assertEqual(result.status_code, 201)
        self.assertEqual(ProviderOrder.objects.get().cancellation_policy, {})
