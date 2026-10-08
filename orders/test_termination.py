from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from backoffice import tests as fixtures
from backoffice.models import ProviderOrderAfterSalesCase as Case
from .models import ProviderOrder, ProviderOrderRefundOrder, ProviderOrderSettlement, ProviderOrderDistribution
from .services import _complete_provider_order_refund, advance_provider_order_settlement, auto_confirm_provider_order


class TerminationTests(APITestCase):
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    @classmethod
    def setUpTestData(cls):
        fixtures.BackofficeProviderReviewTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.order = self.create_fulfillment_order(order_no="TERM-1", provider=self.handan,
                                                  status=ProviderOrder.Status.IN_SERVICE)
        self.order.pricing_snapshot = {"platform_commission_rate": "30"}
        self.order.service_fee_amount = 15000
        self.order.transport_fee_amount = 1800
        self.order.save()
        self.ended_at = timezone.now() - timedelta(minutes=5)
        self.role.permissions = ["order.after_sales.review", "order.after_sales.view", "refund.supervise"]
        self.role.save()
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("No live channel calls"))
        guard.start()
        self.addCleanup(guard.stop)

    def submit(self, *, role="customer", **overrides):
        self.client.force_authenticate(self.order.customer if role == "customer" else self.order.provider.user)
        prefix = "provider-orders" if role == "customer" else "providers/me/orders"
        return self.client.post(f"/api/v1/{prefix}/{self.order.order_no}/termination/", {
            "ended_at": self.ended_at.isoformat(), "reason": "达人提前离场无法继续服务", "evidence_asset_ids": [], **overrides,
        }, format="json")

    def decision(self, components=None, **overrides):
        case = Case.objects.get(order=self.order)
        self.client.force_authenticate(self.admin_user)
        return self.client.post(reverse("backoffice-provider-order-after-sales-action", args=(case.case_no,)), {
            "action": "resolve_termination", "ended_at": self.ended_at.isoformat(),
            "responsibility": "provider", "component_refunds": components or {"service": 7500, "transport": 900, "other": 0},
            "result_note": "核对双方记录后按实际履约情况裁定", **overrides,
        }, format="json")

    def complete_refund(self):
        refund = ProviderOrderRefundOrder.objects.get(order=self.order)
        return _complete_provider_order_refund(refund.refund_no, gateway_refund_no="MOCK-TERM",
                                               refunded_at=timezone.now())

    def test_request_freezes_order_and_is_idempotent_for_both_parties(self):
        self.assertEqual(self.submit().status_code, 201)
        self.assertEqual(self.submit(role="provider").status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "after_sales")
        self.assertEqual(Case.objects.filter(order=self.order).count(), 1)
        auto_confirm_provider_order(order_no=self.order.order_no, now=timezone.now() + timedelta(days=30))
        self.order.refresh_from_db()
        self.assertIsNone(self.order.auto_confirmed_at)
        self.assertFalse(self.order.refund_orders.exists())

    def test_provider_can_apply(self):
        response = self.submit(role="provider")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Case.objects.get(order=self.order).creator, self.order.provider.user)

    def test_invalid_time_and_unstarted_order_rejected(self):
        self.assertEqual(self.submit(ended_at=(timezone.now() + timedelta(hours=1)).isoformat()).status_code, 400)
        self.assertEqual(self.submit(ended_at=(self.order.service_started_at - timedelta(minutes=1)).isoformat()).status_code, 400)
        self.order.status = "pending_service"
        self.order.save()
        self.assertEqual(self.submit().status_code, 400)

    def test_ownership(self):
        self.client.force_authenticate(self.admin_user)
        response = self.client.post(f"/api/v1/provider-orders/{self.order.order_no}/termination/", {
            "ended_at": self.ended_at.isoformat(), "reason": "越权申请提前终止服务",
        }, format="json")
        self.assertEqual(response.status_code, 404)

    def test_partial_refund_preserves_termination_and_original_split(self):
        self.submit()
        response = self.decision()
        self.assertEqual(response.status_code, 200, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated")
        self.assertFalse(ProviderOrderSettlement.objects.filter(order=self.order).exists())
        self.complete_refund()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated")
        self.assertIsNone(self.order.customer_confirmed_at)
        settlement = ProviderOrderSettlement.objects.get(order=self.order)
        self.assertEqual(settlement.provider_settlement_amount, 6150)
        self.assertEqual(settlement.platform_commission_amount, 2250)
        self.assertEqual(settlement.net_transport_fee_amount, 900)
        self.assertEqual(settlement.status, "dispute_frozen")
        self.assertTrue(settlement.distribution_plan.requires_manual_review)
        self.assertIn("termination_reconciliation", [x["code"] for x in settlement.distribution_plan.blockers])
        self.assertFalse(self.complete_refund()[1])
        self.assertEqual(advance_provider_order_settlement(order_no=self.order.order_no)["state"], "termination_reconciliation")
        self.assertEqual(self.decision().status_code, 400)
        self.assertEqual(self.order.refund_orders.count(), 1)

    def test_full_refund_remains_terminated_and_cancels_settlement(self):
        self.submit()
        self.assertEqual(self.decision({"service": 15000, "transport": 1800, "other": 0}).status_code, 200)
        self.complete_refund()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated")
        self.assertEqual(self.order.settlement.status, "cancelled")
        self.assertEqual(self.order.settlement.provider_settlement_amount, 0)

    def test_zero_refund_ends_service_without_fake_refund_or_payout(self):
        self.submit()
        response = self.decision({"service": 0, "transport": 0, "other": 0})
        self.assertEqual(response.status_code, 200, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "terminated")
        self.assertFalse(self.order.refund_orders.exists())
        self.assertEqual(Case.objects.get(order=self.order).status, "resolved")
        self.assertEqual(self.order.settlement.status, "dispute_frozen")

    def test_transport_refund_can_be_adjudicated_independently(self):
        self.submit()
        self.assertEqual(self.decision({"service": 0, "transport": 800, "other": 0}).status_code, 200)
        refund = self.order.refund_orders.get()
        self.assertEqual(refund.transport_fee_refund_amount, 800)
        self.assertEqual(refund.service_fee_refund_amount, 0)

    def test_component_over_refund_and_missing_decision_rejected(self):
        self.submit()
        self.assertEqual(self.decision({"service": 0, "transport": 1801, "other": 0}).status_code, 400)
        self.assertEqual(self.decision(action="approve", approved_amount=100).status_code, 400)
        self.assertEqual(self.decision(responsibility="").status_code, 400)
        self.assertFalse(self.order.refund_orders.exists())

    def test_approval_permission_and_quota_escalation(self):
        self.submit()
        self.role.permissions = ["order.after_sales.review"]
        self.role.save()
        self.assertEqual(self.decision().status_code, 403)
        self.role.permissions.append("refund.approve")
        self.role.save()
        response = self.decision()
        self.assertEqual(response.status_code, 200, response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "after_sales")
        case = Case.objects.get(order=self.order)
        self.assertTrue(case.requires_supervisor)
        self.assertNotIn("decision", case.termination_snapshot)
        self.assertFalse(self.order.refund_orders.exists())

    def test_rejection_restores_only_unadjudicated_request(self):
        self.submit()
        self.assertEqual(self.decision(action="reject").status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "in_service")
        self.assertFalse(self.order.refund_orders.exists())

    def test_pending_confirmation_timer_pauses_until_rejection(self):
        self.order.status = "pending_confirmation"
        self.order.confirmation_expires_at = timezone.now() + timedelta(hours=1)
        self.order.save()
        self.submit()
        self.order.refresh_from_db()
        self.assertIsNone(self.order.confirmation_expires_at)
        self.assertGreater(self.order.confirmation_remaining_seconds, 3500)
        future = timezone.now() + timedelta(days=4)
        with patch("django.utils.timezone.now", return_value=future):
            self.assertEqual(self.decision(action="reject").status_code, 200)
        self.order.refresh_from_db()
        self.assertGreater(self.order.confirmation_expires_at, future + timedelta(minutes=58))

    def test_scope_evidence_and_credit_unchanged(self):
        from mediafiles.models import MediaAsset
        asset = MediaAsset.objects.create(owner=self.admin_user, scope="private", category="support_attachment",
                                          status="uploaded", object_key="private/test-proof.jpg")
        self.assertEqual(self.submit(evidence_asset_ids=[str(asset.pk)]).status_code, 400)
        self.submit()
        original = self.handan.credit_score
        self.decision()
        self.handan.refresh_from_db()
        self.assertEqual(self.handan.credit_score, original)

    def test_channel_started_blocks_request_even_if_distribution_unknown(self):
        settlement = ProviderOrderSettlement.objects.create(order=self.order, provider=self.handan,
            paid_amount=16800, refunded_amount=0, net_service_fee_amount=15000, net_transport_fee_amount=1800, net_other_fee_amount=0,
            platform_commission_rate=30, platform_commission_amount=4500, provider_service_income_amount=10500,
            provider_settlement_amount=12300, frozen_at=timezone.now(), freeze_until=timezone.now())
        ProviderOrderDistribution.objects.create(settlement=settlement, status="unknown", req_seq_id="TERM-UNKNOWN",
                                                 snapshot={}, payment_fee_amount=0)
        self.assertEqual(self.submit().status_code, 400)
        self.assertFalse(Case.objects.filter(order=self.order).exists())

    def test_remaining_funds_queue_and_terminal_guards(self):
        from backoffice.operations_queue import get_queue
        from backoffice.access import resolve_admin_access
        from .fulfillment import resolve_fulfillment_review
        from rest_framework.exceptions import ValidationError
        self.submit()
        self.decision({"service": 0, "transport": 0, "other": 0})
        self.role.permissions.append("order.finance.view")
        self.role.save()
        access = resolve_admin_access(self.admin_user)
        queue = get_queue(access, "termination_reconciliation")
        self.assertEqual(queue.queryset.count(), 1)
        self.client.force_authenticate(self.admin_user)
        response = self.client.get(reverse("backoffice-provider-order-after-sales"), {"todo": "termination_reconciliation"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)
        self.order.refresh_from_db()
        with self.assertRaises(ValidationError):
            resolve_fulfillment_review(self.order, revision=self.order.fulfillment_revision,
                                       reason="不能通过普通履约审核恢复终止订单", actor=self.admin_user)
        self.client.force_authenticate(self.order.customer)
        response = self.client.post(f"/api/v1/provider-orders/{self.order.order_no}/after-sales/", {
            "case_type": "refund", "requested_amount": 100, "reason": "终止订单不得恢复普通售后",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_provider_gets_request_and_result_notifications(self):
        from notifications.models import UserNotification
        self.submit()
        self.decision({"service": 0, "transport": 0, "other": 0})
        notices = UserNotification.objects.filter(recipient=self.handan.user, target_id=self.order.order_no)
        self.assertEqual(notices.count(), 2)
        self.assertTrue(all("order_no=" in item.action_url for item in notices))

    def test_missing_original_rate_does_not_use_current_category_rate(self):
        self.order.pricing_snapshot = {}
        self.order.save()
        self.submit()
        self.assertEqual(self.decision().status_code, 400)
        self.assertFalse(self.order.refund_orders.exists())
        self.assertEqual(self.decision({"service": 15000, "transport": 1800, "other": 0}).status_code, 200)
        self.complete_refund()
        self.assertEqual(ProviderOrderSettlement.objects.get(order=self.order).provider_settlement_amount, 0)

    def test_decision_rate_is_frozen_and_not_exposed_to_customer(self):
        self.submit()
        response = self.decision()
        self.assertNotIn("platform_commission_rate", response.data["data"]["termination"]["decision"])
        self.order.pricing_snapshot = {"platform_commission_rate": "90"}
        self.order.save(update_fields=("pricing_snapshot",))
        self.complete_refund()
        self.assertEqual(ProviderOrderSettlement.objects.get(order=self.order).platform_commission_amount, 2250)
