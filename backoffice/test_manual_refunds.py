"""Manual records only: isolated database and forbidden real network calls."""
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from orders.distributions import _snapshot, assert_refund_not_distributed
from orders.models import ProviderOrder, ProviderOrderDistribution, ProviderOrderSettlement
from orders.services import create_provider_order_refund
from orders.serializers import ProviderOrderSerializer
from providers.models import ProviderIncomeEntry
from taskcenter.models import ScheduledTask
from . import tests as fixtures
from .access import resolve_admin_access
from .manual_refunds import context
from .models import AdminAuditLog, ProviderManualRefundCase as Case
from .operations_queue import work_summary


class ManualRefundTests(APITestCase):
    setUpTestData = classmethod(fixtures.BackofficeProviderReviewTests.setUpTestData.__func__)
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    def setUp(self):
        self.client.force_authenticate(self.admin_user)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("No live network"))
        guard.start()
        self.addCleanup(guard.stop)

    def order(self, number="MANUAL-ORDER", provider=None, settled=True):
        order = self.create_fulfillment_order(order_no=number, provider=provider or self.handan,
                                             status=ProviderOrder.Status.COMPLETED)
        if settled:
            self.settle(order)
        return order

    def settle(self, order):
        return ProviderOrderSettlement.objects.create(order=order, provider=order.provider,
            paid_amount=16800, net_service_fee_amount=16800, net_transport_fee_amount=0,
            net_other_fee_amount=0, platform_commission_amount=3360,
            provider_service_income_amount=13440, provider_settlement_amount=13440,
            status="settled", frozen_at=timezone.now(), freeze_until=timezone.now())

    def create(self, order, amount=1000, key=None):
        return self.client.post(reverse("admin-manual-refunds"), {
            "order_no": order.order_no, "requested_amount": amount,
            "request_key": str(key or uuid4()), "reason": "已核实用户诉求，转财务人工处理",
        }, format="json")

    def new(self, order=None, amount=1000):
        response = self.create(order or self.order(), amount)
        self.assertEqual(response.status_code, 201, response.data)
        return Case.objects.get(case_no=response.data["data"]["case_no"])

    def act(self, case, action, **extra):
        case.refresh_from_db()
        return self.client.post(reverse("admin-manual-refund-detail", args=(case.case_no,)), {
            "action": action, "revision": case.revision, "note": "已核实凭证金额与收款用户一致", **extra,
        }, format="json")

    def evidence(self, case, receipt="BANK-TEST-001", **extra):
        return self.act(case, "submit_evidence", receipt_no=receipt,
            evidence_reference="内部财务回单归档/测试凭证001",
            paid_at=(timezone.now() - timedelta(minutes=1)).isoformat(), **extra)

    def reviewing(self, case):
        self.assertEqual(self.act(case, "start").status_code, 200)
        response = self.evidence(case)
        self.assertEqual(response.status_code, 200, response.data)

    def test_create_is_idempotent_and_reserves_amount_without_money_mutations(self):
        order = self.order()
        original_order = ProviderOrder.objects.values().get(pk=order.pk)
        original_payment = type(order.payment_order).objects.values().get(order=order)
        original_settlement = ProviderOrderSettlement.objects.values().get(order=order)
        task_count, income_count = ScheduledTask.objects.count(), ProviderIncomeEntry.objects.count()
        key = uuid4()
        one, two = self.create(order, key=key), self.create(order, key=key)
        self.assertEqual(one.status_code, 201, one.data)
        self.assertEqual(two.status_code, 200, two.data)
        self.assertEqual(one.data["data"]["case_no"], two.data["data"]["case_no"])
        self.assertEqual(Case.objects.count(), 1)
        self.assertEqual(AdminAuditLog.objects.filter(action="manual_refund.create").count(), 1)
        self.assertEqual(self.create(order, amount=900, key=key).status_code, 400)
        self.assertEqual(self.create(order).status_code, 400)
        self.assertEqual(context(order)["occupied_amount"], 1000)
        self.assertEqual(context(order)["remaining"], 15800)
        self.assertEqual(ProviderOrder.objects.values().get(pk=order.pk), original_order)
        self.assertEqual(type(order.payment_order).objects.values().get(order=order), original_payment)
        self.assertEqual(ProviderOrderSettlement.objects.values().get(order=order), original_settlement)
        self.assertFalse(order.refund_orders.exists())
        self.assertEqual(ScheduledTask.objects.count(), task_count)
        self.assertEqual(ProviderIncomeEntry.objects.count(), income_count)

    def test_invalid_amounts_and_unsettled_or_unpaid_orders_fail_closed(self):
        order = self.order()
        for amount in (0, -1, 16801, "1.5", True, 2147483648):
            self.assertEqual(self.create(order, amount).status_code, 400, amount)
        self.assertEqual(self.create(self.order("UNSETTLED", settled=False)).status_code, 400)
        payment = order.payment_order
        payment.status = "pending_payment"
        payment.save()
        self.assertEqual(self.create(order).status_code, 400)
        self.assertFalse(Case.objects.exists())

    def test_city_scope_permissions_and_anonymous_access(self):
        order = self.order()
        foreign = self.order("FOREIGN", self.beijing)
        self.assertEqual(self.create(foreign).status_code, 404)
        self.client.force_authenticate(self.platform_admin)
        case = self.new(foreign)
        self.client.force_authenticate(self.admin_user)
        url = reverse("admin-manual-refund-detail", args=(case.case_no,))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.act(case, "start").status_code, 404)
        self.assertEqual(self.client.get(reverse("admin-manual-refunds")).data["data"]["total"], 0)
        self.role.permissions.remove("order.after_sales.create")
        self.role.save()
        self.assertEqual(self.create(order).status_code, 403)
        self.role.permissions = []
        self.role.save()
        self.assertEqual(self.client.get(reverse("admin-manual-refunds")).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_full_lifecycle_requires_independent_reviewer_and_preserves_channel(self):
        order = self.order()
        case = self.new(order)
        self.reviewing(case)
        self.assertEqual(self.act(case, "verify").status_code, 400)
        self.client.force_authenticate(self.platform_admin)
        response = self.act(case, "verify")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["actions"], [])
        self.assertEqual(len(response.data["data"]["records"]), 4)
        self.assertEqual(context(order)["completed_amount"], 1000)
        self.assertEqual(context(order)["occupied_amount"], 0)
        self.assertEqual(self.act(case, "verify").status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_order.status, "paid")
        self.assertEqual(order.settlement.refunded_amount, 0)
        self.assertEqual(order.settlement.status, "settled")
        self.assertFalse(order.refund_orders.exists())
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        self.assertEqual(self.create(order, 15801).status_code, 400)
        self.assertEqual(self.create(order, 15800).status_code, 201)

    def test_finance_and_supervisor_permissions_are_separate(self):
        case = self.new()
        self.role.permissions.remove("order.finance.manage")
        self.role.save()
        self.assertEqual(self.act(case, "start").status_code, 403)
        self.role.permissions.append("order.finance.manage")
        self.role.permissions.remove("refund.supervise")
        self.role.save()
        self.assertEqual(self.act(case, "reject").status_code, 403)
        self.assertEqual(self.act(case, "start").status_code, 200)

    def test_stale_revision_and_missing_or_future_evidence_rejected(self):
        case = self.new()
        self.assertEqual(self.act(case, "start").status_code, 200)
        self.assertEqual(self.act(case, "submit_evidence", revision=1).status_code, 400)
        self.assertEqual(self.act(case, "submit_evidence").status_code, 400)
        for date in (timezone.now() + timedelta(days=1), timezone.now() - timedelta(days=2)):
            response = self.act(case, "submit_evidence", receipt_no="TEST",
                evidence_reference="internal-proof", paid_at=date.isoformat())
            self.assertEqual(response.status_code, 400)
        case.refresh_from_db()
        self.assertEqual(case.status, "processing")
        self.assertEqual(case.revision, 2)

    def test_return_keeps_reservation_and_original_receipt_cannot_be_replaced(self):
        case = self.new()
        self.reviewing(case)
        self.client.force_authenticate(self.platform_admin)
        self.assertEqual(self.act(case, "return").status_code, 200)
        self.assertEqual(context(case.order)["occupied_amount"], 1000)
        self.assertEqual(self.act(case, "reject").status_code, 400)
        self.assertEqual(self.evidence(case, receipt="REPLACEMENT").status_code, 400)
        self.client.force_authenticate(self.admin_user)
        self.assertEqual(self.evidence(case).status_code, 200)
        self.client.force_authenticate(self.platform_admin)
        self.assertEqual(self.act(case, "verify").status_code, 200)
        other = self.new(self.order("SECOND"))
        self.assertEqual(self.act(other, "start").status_code, 200)
        self.assertEqual(self.evidence(other).status_code, 400)

    def test_reject_before_payout_releases_amount_and_closed_case_is_immutable(self):
        case = self.new()
        response = self.act(case, "reject")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(context(case.order)["remaining"], 16800)
        self.assertEqual(self.act(case, "start").status_code, 400)
        self.assertEqual(self.create(case.order, 16800).status_code, 201)

    def test_automatic_refund_and_distribution_paths_remain_blocked(self):
        order = self.order()
        case = self.new(order)
        for status in ("pending", "processing", "reviewing", "completed"):
            Case.objects.filter(pk=case.pk).update(status=status)
            with self.assertRaisesMessage(ValidationError, "人工售后"):
                assert_refund_not_distributed(order)
            with self.assertRaisesMessage(ValidationError, "人工售后"):
                _snapshot(order, None, now=timezone.now())
            with self.assertRaisesMessage(ValidationError, "人工售后"):
                create_provider_order_refund(order_no=order.order_no, amount=1000,
                    source_type="after_sales", source_reference="AS-TEST", idempotency_key="BLOCKED",
                    reason="已有人工退款，不得自动退款")
        self.assertFalse(order.refund_orders.exists())

    def test_unknown_distribution_can_register_but_cannot_claim_verified_payout(self):
        order = self.order()
        distribution = ProviderOrderDistribution.objects.create(settlement=order.settlement,
            req_date="20261018", req_seq_id="OFFLINE", status="unknown", snapshot={}, payment_fee_amount=0)
        case = self.new(order)
        self.assertEqual(self.act(case, "start").status_code, 200)
        self.assertEqual(self.evidence(case).status_code, 400)
        distribution.status = "succeeded"
        distribution.save()
        self.assertEqual(self.evidence(case).status_code, 200)
        distribution.refresh_from_db()
        self.assertEqual(distribution.status, "succeeded")
        distribution.evidence_conflict_code = "query_amount_conflict"
        distribution.save()
        self.client.force_authenticate(self.platform_admin)
        self.assertEqual(self.act(case, "verify").status_code, 400)

    def test_queue_counts_filters_and_status_revision_refresh(self):
        case = self.new()
        def todo(key):
            data = work_summary(resolve_admin_access(self.admin_user), self.admin_user)
            return next(item for item in data["todos"] if item["key"] == key)
        self.assertEqual(todo("manual_refund_processing")["count"], 1)
        self.assertEqual(todo("manual_refund_review")["count"], 0)
        self.reviewing(case)
        self.assertEqual(todo("manual_refund_processing")["count"], 0)
        self.assertEqual(todo("manual_refund_review")["count"], 1)
        response = self.client.get(reverse("admin-manual-refunds"), {"todo": "manual_refund_review", "search": case.order.order_no})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["total"], 1)
        self.assertEqual(self.client.get(reverse("admin-manual-refunds"), {"todo": "provider_after_sales"}).status_code, 404)
        self.client.force_authenticate(self.platform_admin)
        self.assertEqual(self.act(case, "verify").status_code, 200)
        self.assertEqual(todo("manual_refund_review")["count"], 0)

    def test_context_exposes_manual_option_not_automatic_refund(self):
        order = self.order()
        url = reverse("admin-refund-context", args=("provider", order.order_no))
        response = self.client.get(url)
        self.assertFalse(response.data["data"]["can_create"])
        self.assertTrue(response.data["data"]["manual"]["can_create"])
        self.new(order)
        response = self.client.get(url)
        self.assertFalse(response.data["data"]["manual"]["can_create"])
        self.assertEqual(response.data["data"]["manual"]["occupied_amount"], 1000)

    def test_list_pagination_and_invalid_filters(self):
        for index in range(21):
            Case.objects.create(order=self.order(f"PAGE-{index}"), created_by=self.admin_user,
                request_key=uuid4(), requested_amount=100, reason="分页测试人工申请")
        response = self.client.get(reverse("admin-manual-refunds"), {"page": 2})
        self.assertEqual(response.data["data"]["total"], 21)
        self.assertEqual(len(response.data["data"]["items"]), 1)
        for params in ({"page": 0}, {"page": "x"}, {"status": "invalid"}):
            self.assertEqual(self.client.get(reverse("admin-manual-refunds"), params).status_code, 400)

    def test_automatic_refund_occupancy_and_success_are_both_deducted(self):
        order = self.order(settled=False)
        refund, _ = create_provider_order_refund(order_no=order.order_no, amount=1000,
            source_type="after_sales", source_reference="OLD-CASE", idempotency_key="OLD-REFUND",
            reason="测试先前退款记录占用")
        # Simulate an existing settled legacy record; no payment/settlement jobs run.
        self.settle(order)
        self.assertEqual(self.create(order).status_code, 400)
        self.assertIn("自动退款", context(order)["blocked_reason"])
        refund.status = "succeeded"
        refund.save()
        self.assertEqual(context(order)["remaining"], 15800)
        self.assertEqual(self.create(order, 15801).status_code, 400)
        self.assertEqual(self.create(order, 15800).status_code, 201)

    def test_creator_cannot_review_another_finance_operators_receipt(self):
        case = self.new()
        self.client.force_authenticate(self.platform_admin)
        self.reviewing(case)
        self.assertEqual(self.act(case, "verify").status_code, 400)
        self.client.force_authenticate(self.admin_user)
        self.assertEqual(self.act(case, "verify").status_code, 400)

    def test_previous_evidence_submitter_cannot_review_after_handover(self):
        case = self.new()
        self.client.force_authenticate(self.platform_admin)
        self.reviewing(case)
        other = get_user_model().objects.create_superuser(
            phone="19900009991", password="offline-only", nickname="另一名财务人员",
        )
        self.client.force_authenticate(other)
        self.assertEqual(self.act(case, "return").status_code, 200)
        self.assertEqual(self.evidence(case).status_code, 200)
        self.client.force_authenticate(self.platform_admin)
        detail = self.client.get(reverse("admin-manual-refund-detail", args=(case.case_no,)))
        self.assertNotIn("verify", detail.data["data"]["actions"])
        self.assertEqual(self.act(case, "verify").status_code, 400)
        self.assertEqual(self.act(case, "return").status_code, 400)

    def test_customer_capability_has_no_internal_finance_details(self):
        order = self.order()
        self.new(order)
        data = ProviderOrderSerializer(order).data
        self.assertTrue(data["manual_after_sales_required"])
        self.assertNotIn("manual_refund_cases", data)
        self.assertNotIn("receipt_no", data)
        fresh = self.order("NO-SETTLEMENT", settled=False)
        self.assertFalse(ProviderOrderSerializer(fresh).data["manual_after_sales_required"])
