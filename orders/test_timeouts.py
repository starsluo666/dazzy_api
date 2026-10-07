"""No-show policy regressions. Payments are synthetic; no real gateway calls."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError

from accounts.models import User
from backoffice.models import (AdminAuditLog, AdminRole, Organization, OrganizationMember,
                               PlatformOperationSetting)
from backoffice.serializers import PlatformOperationSettingSerializer, ProviderCreditAdjustmentSerializer
from notifications.models import UserNotification
from taskcenter.models import ScheduledTask
from taskcenter.services import synchronize_provider_fulfillment_timeouts, TASK_HANDLERS
from . import tests as fixtures
from .fulfillment import resolve_fulfillment_review
from .models import ProviderOrder
from .services import _complete_provider_order_refund, create_provider_order_refund
from .timeouts import (expire_provider_departure, inspect_idle_fulfillment,
                       reverse_departure_penalty, timeout_summary, legacy_overdue_query)


@override_settings(DEBUG=True)
class OrderTimeoutTests(TestCase):
    def setUp(self):
        fixtures.ProviderOrderApiTests.setUp(self)
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Offline test forbids real HTTP"))
        network.start()
        self.addCleanup(network.stop)
    payload = fixtures.ProviderOrderApiTests.payload
    create_paid_accepted_order = fixtures.ProviderOrderApiTests.create_paid_accepted_order

    def test_new_order_snapshot_and_config_validation(self):
        setting = PlatformOperationSetting.current()
        setting.provider_order_departure_grace_minutes = 45
        setting.provider_order_no_departure_credit_penalty = 3
        setting.save()
        order = self.create_paid_accepted_order()
        self.assertEqual(order.departure_deadline_at, order.starts_at + timedelta(minutes=45))
        self.assertEqual(order.start_deadline_at, order.starts_at + timedelta(minutes=30))
        setting.provider_order_no_departure_credit_penalty = 9
        setting.save()
        expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        order.refresh_from_db()
        self.assertEqual(order.timeout_credit_points, 3)
        for field, value in (("provider_order_departure_grace_minutes", -1),
                             ("provider_order_departure_grace_minutes", 181),
                             ("provider_order_no_departure_credit_penalty", 101)):
            serializer = PlatformOperationSettingSerializer(setting, data={field: value}, partial=True)
            self.assertFalse(serializer.is_valid())

    def test_deadline_full_actual_refund_includes_transport_and_penalty_once(self):
        order = self.create_paid_accepted_order()
        now = order.departure_deadline_at
        self.assertEqual(expire_provider_departure(order.order_no, now=now - timedelta(seconds=1))["state"], "not_due")
        self.assertEqual(expire_provider_departure(order.order_no, now=now)["state"], "expired")
        self.assertEqual(expire_provider_departure(order.order_no, now=now + timedelta(days=1))["state"], "already_expired")
        order.refresh_from_db()
        self.provider.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.provider.credit_score, 98)
        refund = order.refund_orders.get()
        self.assertEqual(refund.refund_amount, order.payable_amount)
        self.assertGreater(refund.transport_fee_refund_amount, 0)
        self.assertEqual(timeout_summary(order)["refund_label"], "退款处理中")
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="OFFLINE-REFUND", refunded_at=now)
        order.refresh_from_db()
        self.assertEqual(order.status, "refunded")
        self.assertEqual(timeout_summary(order)["refund_label"], "已退款")
        self.assertEqual(order.credit_adjustments.count(), 1)
        self.assertEqual(ProviderCreditAdjustmentSerializer(order.credit_adjustments.get()).data["operator_name"], "系统")

    def test_depart_action_guard_commits_cancellation_instead_of_rolling_it_back(self):
        order = self.create_paid_accepted_order()
        order.provider_contact_initiated_at = order.accepted_at
        order.save()
        with patch("orders.views.timezone.now", return_value=order.departure_deadline_at):
            response = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/depart/",
                                        {"contact_confirmed": True}, content_type="application/json")
        self.assertEqual(response.status_code, 409, response.content)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.refund_orders.count(), 1)
        self.assertIsNone(order.departed_at)

    def test_depart_first_wins_then_overdue_service_goes_to_manual_review(self):
        order = self.create_paid_accepted_order()
        order.provider_contact_initiated_at = order.accepted_at
        order.save()
        with patch("orders.views.timezone.now", return_value=order.departure_deadline_at - timedelta(seconds=1)):
            response = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/depart/",
                                       {"contact_confirmed": True}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(expire_provider_departure(order.order_no, now=order.departure_deadline_at)["state"], "not_applicable")
        inspect_idle_fulfillment(order.order_no, stage="start", now=order.start_deadline_at + timedelta(seconds=1))
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertFalse(order.refund_orders.exists())
        self.assertFalse(order.credit_adjustments.exists())
        with transaction.atomic():
            resolve_fulfillment_review(order, actor=self.customer, reason="已核实双方到场", revision=order.fulfillment_revision)
        inspect_idle_fulfillment(order.order_no, stage="start", now=order.start_deadline_at + timedelta(days=1))
        order.refresh_from_db()
        self.assertFalse(order.fulfillment_review_required)
        self.assertEqual(len(order.fulfillment_issues), 1)

    def test_floor_zero_and_appeal_reverses_only_actual_deduction_once(self):
        self.provider.credit_score = 1
        self.provider.save()
        order = self.create_paid_accepted_order()
        expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        order.refresh_from_db()
        self.assertEqual(order.timeout_credit_points, 1)
        with transaction.atomic():
            self.assertTrue(reverse_departure_penalty(order, actor=self.customer, reason="核实系平台故障"))
            self.assertFalse(reverse_departure_penalty(order, actor=self.customer, reason="重复申诉"))
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.credit_score, 1)
        self.assertEqual(order.credit_adjustments.count(), 2)
        self.assertEqual(order.status, "cancelled")
        with transaction.atomic(), self.assertRaises(ValidationError):
            resolve_fulfillment_review(order, actor=self.customer, revision=1, reason="不可恢复退款单")

    def test_legacy_is_triaged_not_enrolled_or_penalized(self):
        order = self.create_paid_accepted_order()
        order.departure_deadline_at = None
        order.starts_at -= timedelta(days=3)
        order.ends_at -= timedelta(days=3)
        order.save()
        ScheduledTask.objects.filter(business_key=order.order_no).delete()
        self.assertEqual(expire_provider_departure(order.order_no)["state"], "not_enrolled")
        self.assertEqual(synchronize_provider_fulfillment_timeouts(batch_size=500), 0)
        self.assertTrue(ProviderOrder.objects.filter(legacy_overdue_query(), pk=order.pk).exists())
        self.assertFalse(order.refund_orders.exists())
        self.assertFalse(order.credit_adjustments.exists())

    def test_existing_pending_or_failed_refund_goes_to_review_without_duplicate(self):
        order = self.create_paid_accepted_order()
        refund, _ = create_provider_order_refund(order_no=order.order_no, amount=100,
            source_type="system", source_reference="existing", idempotency_key="existing", reason="原退款")
        for status in ("pending", "failed"):
            refund.status = status
            refund.save()
            self.assertEqual(expire_provider_departure(order.order_no, now=order.departure_deadline_at)["state"], "manual_review")
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertEqual(order.refund_orders.count(), 1)
        self.assertFalse(order.credit_adjustments.exists())

    def test_succeeded_partial_refund_only_refunds_remainder(self):
        order = self.create_paid_accepted_order()
        refund, _ = create_provider_order_refund(order_no=order.order_no, amount=100,
            source_type="system", source_reference="partial", idempotency_key="partial", reason="部分退款")
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="PARTIAL", refunded_at=order.accepted_at)
        outcome = expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        self.assertEqual(outcome["refund_amount"], order.payable_amount - 100)

    def test_financial_validation_failure_is_review_not_partial_deduction(self):
        order = self.create_paid_accepted_order()
        with patch("orders.distributions.assert_refund_not_distributed", side_effect=ValidationError("分账中")):
            result = expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        self.assertEqual(result["state"], "manual_review")
        self.assertFalse(order.credit_adjustments.exists())
        self.assertFalse(order.refund_orders.exists())

    def test_completion_timeout_and_idempotent_reminder(self):
        order = self.create_paid_accepted_order()
        for _ in range(2):
            inspect_idle_fulfillment(order.order_no, stage="reminder", now=order.starts_at - timedelta(minutes=30))
        self.assertEqual(UserNotification.objects.filter(event_type="order_departure_reminder", recipient=self.provider_user).count(), 1)
        order.status = "in_service"
        order.service_started_at = order.starts_at
        order.save()
        for _ in range(2):
            inspect_idle_fulfillment(order.order_no, stage="completion", now=order.completion_deadline_at + timedelta(seconds=1))
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertEqual(len(order.fulfillment_issues), 1)
        self.assertFalse(order.refund_orders.exists())

    def test_reconciliation_and_worker_register_missing_tasks_only(self):
        order = self.create_paid_accepted_order()
        ScheduledTask.objects.filter(business_key=order.order_no, task_type__in=(
            ScheduledTask.Type.PROVIDER_DEPARTURE_TIMEOUT, ScheduledTask.Type.PROVIDER_DEPARTURE_REMINDER)).delete()
        self.assertEqual(synchronize_provider_fulfillment_timeouts(batch_size=1), 2)
        self.assertEqual(synchronize_provider_fulfillment_timeouts(batch_size=1), 0)
        task = ScheduledTask.objects.get(business_key=order.order_no, task_type=ScheduledTask.Type.PROVIDER_DEPARTURE_TIMEOUT)
        handler = TASK_HANDLERS[task.task_type]
        self.assertEqual(handler(task, order.departure_deadline_at - timedelta(seconds=1)).status, "pending")
        self.assertEqual(handler(task, order.departure_deadline_at).status, "succeeded")
        self.assertEqual(handler(task, order.departure_deadline_at).status, "succeeded")
        self.assertEqual(order.credit_adjustments.count(), 1)

    def test_existing_evidence_or_payment_inconsistency_never_automatically_refunds(self):
        order = self.create_paid_accepted_order()
        order.service_started_at = order.starts_at
        order.save()
        self.assertEqual(expire_provider_departure(order.order_no, now=order.departure_deadline_at)["state"], "manual_review")
        self.assertFalse(order.refund_orders.exists())
        self.assertFalse(order.credit_adjustments.exists())

    def test_customer_support_case_freezes_automatic_financial_decision(self):
        from supportcases.models import SupportCase
        order = self.create_paid_accepted_order()
        SupportCase.objects.create(reporter=self.customer, provider_order=order, target_type="provider_order",
                                   case_type="complaint", reason="service_quality", description="联系不上达人")
        self.assertEqual(expire_provider_departure(order.order_no, now=order.departure_deadline_at)["state"], "manual_review")
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertFalse(order.refund_orders.exists())

    def test_zero_payment_closes_without_fabricated_refund_and_returns_coupon(self):
        from .models import UserCoupon
        order = self.create_paid_accepted_order()
        order.discount_amount = order.payable_amount
        order.payable_amount = 0
        order.save()
        payment = order.payment_order
        payment.discount_amount = order.discount_amount
        payment.payable_amount = 0
        payment.save()
        coupon = UserCoupon.objects.create(owner=self.customer, face_amount=order.discount_amount,
            min_order_amount=0, expires_at=order.ends_at + timedelta(days=1),
            status="used", reserved_order=order, used_at=order.paid_at)
        result = expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        self.assertEqual(result["refund_amount"], 0)
        order.refresh_from_db()
        coupon.refresh_from_db()
        self.assertEqual(order.status, "refunded")
        self.assertEqual(coupon.status, "available")
        self.assertFalse(order.refund_orders.exists())

    def test_wallet_and_mixed_refund_preserve_original_routes(self):
        from wallets.models import UserWallet, WalletPaymentAllocation
        wallet = UserWallet.objects.create(user=self.customer, available_balance=0)
        for wallet_amount in (100, None):
            self.client.force_login(self.customer)
            order = self.create_paid_accepted_order()
            amount = order.payable_amount if wallet_amount is None else wallet_amount
            WalletPaymentAllocation.objects.update_or_create(business_type="provider_order", business_order_no=order.order_no,
                defaults={"user": self.customer, "payable_amount": order.payable_amount,
                          "wallet_amount": amount, "external_amount": order.payable_amount - amount, "status": "consumed"})
            wallet.refresh_from_db()  # Creating the next paid order may consume the previous refund.
            previous = wallet.available_balance
            expire_provider_departure(order.order_no, now=order.departure_deadline_at)
            refund = order.refund_orders.get()
            self.assertEqual(refund.wallet_refund_amount, amount)
            self.assertEqual(refund.external_refund_amount, order.payable_amount - amount)
            for _ in range(2):
                _complete_provider_order_refund(refund.refund_no, gateway_refund_no=f"OFFLINE-{order.pk}", refunded_at=order.departure_deadline_at)
            wallet.refresh_from_db()
            self.assertEqual(wallet.available_balance, previous + amount)

    def test_refund_task_exhaustion_is_shown_as_attention_not_success(self):
        from .timeouts import refund_attention_query
        order = self.create_paid_accepted_order()
        expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        refund = order.refund_orders.get()
        refund.status = "processing"
        refund.save()
        ScheduledTask.objects.filter(task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND, business_key=refund.refund_no).update(status="failed")
        order.refresh_from_db()
        self.assertEqual(timeout_summary(order)["refund_label"], "退款异常，客服核实中")
        self.assertTrue(ProviderOrder.objects.filter(refund_attention_query(), pk=order.pk).exists())
        self.assertEqual(order.status, "cancelled")

    def test_exact_completion_tolerance_boundary_is_not_yet_abnormal(self):
        order = self.create_paid_accepted_order()
        order.status = "in_service"
        order.save()
        self.assertEqual(inspect_idle_fulfillment(order.order_no, stage="completion", now=order.completion_deadline_at)["state"], "not_due")
        order.refresh_from_db()
        self.assertFalse(order.fulfillment_review_required)

    def test_unaccepted_or_cancelled_orders_are_not_penalized(self):
        order = self.create_paid_accepted_order()
        for status in ("pending_acceptance", "pending_support", "cancelled", "after_sales", "refunded"):
            order.status = status
            order.save()
            self.assertEqual(expire_provider_departure(order.order_no, now=order.departure_deadline_at)["state"], "not_applicable")
        self.assertFalse(order.credit_adjustments.exists())

    def test_admin_appeal_permissions_city_scope_idempotence_and_filter(self):
        order = self.create_paid_accepted_order()
        expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        staff = User.objects.create_user(phone="19900004568", nickname="申诉客服")
        org = Organization.objects.create(name="测试城市", code="timeout-test", city_codes=["130400"], organization_type="city_agent")
        role = AdminRole.objects.create(organization=org, name="客服", code="cs", data_scope="city",
            permissions=["order.fulfillment.view", "order.fulfillment.review"])
        OrganizationMember.objects.create(user=staff, organization=org, role=role)
        self.client.force_login(staff)
        url = f"/api/v1/admin/provider-orders/{order.order_no}/timeout-appeal/"
        payload = {"reason": "联系双方核实系系统故障"}
        self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 403)
        role.permissions.append("provider.credit.adjust")
        role.save()
        org.city_codes = ["110100"]
        org.save()
        self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 404)
        org.city_codes = ["130400"]
        org.save()
        self.assertEqual(self.client.post(url, {"reason": " "}, content_type="application/json").status_code, 400)
        result = self.client.get("/api/v1/admin/provider-orders/?anomaly=departure_timeout")
        self.assertEqual(result.json()["data"]["pagination"]["total"], 1)
        for _ in range(2):
            response = self.client.post(url, payload, content_type="application/json")
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(AdminAuditLog.objects.filter(target_id=order.order_no, action="provider.credit.adjust").count(), 1)


@skipUnless(connection.vendor == "postgresql", "Requires isolated PostgreSQL row locks")
@override_settings(DEBUG=True)
class OrderTimeoutConcurrencyTests(TransactionTestCase):
    setUp = fixtures.ProviderOrderApiTests.setUp
    payload = fixtures.ProviderOrderApiTests.payload
    create_paid_accepted_order = fixtures.ProviderOrderApiTests.create_paid_accepted_order

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

    def test_duplicate_timeout_only_one_refund_and_penalty(self):
        order = self.create_paid_accepted_order()
        def action():
            return expire_provider_departure(order.order_no, now=order.departure_deadline_at)
        self.race(action, action)
        self.assertEqual(order.refund_orders.count(), 1)
        self.assertEqual(order.credit_adjustments.count(), 1)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.credit_score, 98)

    def test_departure_racing_timeout_never_both_departed_and_refunded(self):
        order = self.create_paid_accepted_order()
        order.provider_contact_initiated_at = order.accepted_at
        order.save()

        def depart():
            client = APIClient()
            client.force_authenticate(self.provider_user)
            return client.post(f"/api/v1/providers/me/orders/{order.order_no}/depart/", {"contact_confirmed": True}).status_code

        with patch("orders.views.timezone.now", return_value=order.departure_deadline_at - timedelta(seconds=1)):
            self.race(depart, lambda: expire_provider_departure(order.order_no, now=order.departure_deadline_at))
        order.refresh_from_db()
        if order.status == "departed":
            self.assertFalse(order.refund_orders.exists())
            self.assertFalse(order.credit_adjustments.exists())
        else:
            self.assertEqual(order.status, "cancelled")
            self.assertIsNone(order.departed_at)
            self.assertEqual(order.refund_orders.count(), 1)

    def test_manual_refund_racing_timeout_never_over_refunds(self):
        order = self.create_paid_accepted_order()

        def manual_refund():
            try:
                create_provider_order_refund(order_no=order.order_no, amount=order.payable_amount,
                    source_type="system", source_reference="manual-race", idempotency_key="manual-race", reason="客服退款")
            except ValidationError:
                pass  # The competing transaction already reserved the refundable amount.

        self.race(manual_refund, lambda: expire_provider_departure(order.order_no, now=order.departure_deadline_at))
        self.assertEqual(order.refund_orders.count(), 1)

    def test_duplicate_appeal_restores_once(self):
        order = self.create_paid_accepted_order()
        expire_provider_departure(order.order_no, now=order.departure_deadline_at)

        def appeal():
            with transaction.atomic():
                current = ProviderOrder.objects.select_for_update().get(pk=order.pk)
                reverse_departure_penalty(current, actor=self.customer, reason="核实平台故障")

        self.race(appeal, appeal)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.credit_score, 100)
        self.assertEqual(order.credit_adjustments.count(), 2)
