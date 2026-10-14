"""Synthetic local regressions; no real channel calls or refunds."""
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from . import tests as fixtures
from .models import AdminAuditLog, PlatformOperationSetting, ProviderOrderAfterSalesCase as Case
from orders.models import ProviderOrder as Order, ProviderOrderRefundOrder as Refund
from orders.models import ProviderOrderPaymentOrder as Payment, ProviderOrderSettlement as Settlement
from orders.cancellations import DEFAULTS, clauses
from orders.services import _complete_provider_order_refund


class FulfillmentAfterSalesTests(APITestCase):
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    @classmethod
    def setUpTestData(cls):
        fixtures.BackofficeProviderReviewTests.setUpTestData.__func__(cls)

    def setUp(self):
        self.client.force_authenticate(self.admin_user)
        self.role.permissions = ["dashboard.view", "order.fulfillment.view", "order.fulfillment.review",
            "order.after_sales.view", "order.after_sales.create", "order.after_sales.review", "order.finance.view", "refund.approve"]
        self.role.save()
        setting = PlatformOperationSetting.current()
        setting.support_refund_single_limit = 100000
        setting.support_refund_daily_limit = 1000000
        setting.save()
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("No live network"))
        guard.start()
        self.addCleanup(guard.stop)

    def make_order(self, number="FULFILLMENT-CASE", **patches):
        order = self.create_fulfillment_order(order_no=number, provider=self.handan)
        order.fulfillment_review_required = True
        order.fulfillment_revision = 1
        order.fulfillment_issues = [{"revision": 1, "code": "duration_short", "label": "实际服务时长不足", "resolved_at": None}]
        order.pricing_snapshot = {"platform_commission_rate": "30"}
        order.service_fee_amount, order.transport_fee_amount, order.payable_amount = 10000, 2000, 12000
        order.transport_mode = "taxi"
        order.cancellation_policy = {"version": "agreed-test-v1", "config": DEFAULTS, "clauses": clauses(DEFAULTS)}
        order.arrived_at = order.starts_at - timedelta(minutes=10)
        for key, value in patches.items():
            setattr(order, key, value)
        order.save()
        Payment.objects.filter(order=order).update(payable_amount=order.payable_amount,
            service_fee_amount=order.service_fee_amount, transport_fee_amount=order.transport_fee_amount,
            other_fee_amount=order.other_fee_amount, discount_amount=order.discount_amount)
        return order

    def intake(self, order, **extra):
        return self.client.post(reverse("admin-fulfillment-after-sales", args=(order.order_no,)),
            {"revision": order.fulfillment_revision, "reason": "双方反映服务未正常完成，转客服核查", **extra}, format="json")

    def new_case(self, order):
        result = self.intake(order)
        self.assertEqual(result.status_code, 201, result.data)
        return result.data["data"]["case_no"]

    def quote(self, order, case_no, **extra):
        return self.client.post(reverse("admin-fulfillment-case-preview", args=(case_no,)), {
            "revision": order.fulfillment_revision, "responsibility": "provider", "mode": "manual",
            "occurred_at": (order.ends_at - timedelta(minutes=30)).isoformat(),
            "component_refunds": {"service": 5000, "transport": 0, "other": 0},
            "facts_confirmed": True, "result_note": "已联系双方核实有效服务及交通费用", **extra,
        }, format="json")

    def resolve(self, case_no, quote):
        self.assertEqual(quote.status_code, 200, quote.data)
        return self.client.post(reverse("admin-fulfillment-case-resolve", args=(case_no,)),
                                {"token": quote.data["data"]["token"]}, format="json")

    def test_transfer_is_idempotent_freezes_and_blocks_normal_recovery(self):
        order = self.make_order()
        case_no = self.new_case(order)
        self.assertEqual(self.intake(order).data["data"]["case_no"], case_no)
        self.assertEqual(Case.objects.count(), 1)
        self.assertEqual(Refund.objects.count(), 0)
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertEqual(order.status, Order.Status.AFTER_SALES)
        self.assertIsNone(order.confirmation_expires_at)
        result = self.client.post(reverse("backoffice-provider-order-fulfillment-review", args=(order.order_no,)),
            {"revision": 1, "reason": "尝试直接恢复正常"}, format="json")
        self.assertEqual(result.status_code, 400)
        context = self.client.get(reverse("admin-fulfillment-case-context", args=(case_no,)))
        self.assertEqual(context.status_code, 200, context.data)
        self.assertEqual(context.data["data"]["transport_mode"], "出租车")
        self.assertTrue(context.data["data"]["can_approve"])

    def test_investigation_preserves_hold_and_records_reason_once(self):
        order = self.make_order()
        for _ in range(2):
            self.assertEqual(self.intake(order, action="investigate").status_code, 200)
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertEqual(len(order.fulfillment_reviews), 1)
        self.assertEqual(order.fulfillment_reviews[0]["outcome"], "investigating")
        self.assertFalse(Case.objects.exists())
        self.assertEqual(AdminAuditLog.objects.filter(action="order.fulfillment.investigate").count(), 1)

    def test_partial_refund_keeps_terminal_status_and_freezes_remaining_funds(self):
        order = self.make_order()
        case_no = self.new_case(order)
        quote = self.quote(order, case_no)
        self.assertEqual(Refund.objects.count(), 0, "preview must not move money")
        result = self.resolve(case_no, quote)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["data"]["status"], "approved")
        self.assertEqual(self.resolve(case_no, quote).status_code, 200)
        refund = Refund.objects.get()
        self.assertEqual(refund.refund_amount, 5000)
        order.refresh_from_db()
        self.assertEqual(order.status, "terminated")
        self.assertFalse(order.fulfillment_review_required)
        self.assertFalse(Settlement.objects.filter(status="settled").exists())
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="TEST", refunded_at=timezone.now())
        order.refresh_from_db()
        self.assertEqual(order.status, "terminated")
        settlement = Settlement.objects.get(order=order)
        self.assertEqual(settlement.status, "dispute_frozen")
        self.assertEqual(settlement.provider_settlement_amount, 5500)
        self.assertEqual(settlement.platform_commission_amount, 1500)
        self.assertEqual(self.resolve(case_no, quote).status_code, 200)
        self.assertEqual(Refund.objects.count(), 1)

    def test_discounted_service_and_order_rule_not_current_settings(self):
        order = self.make_order(discount_amount=2000, payable_amount=10000)
        order.cancellation_policy = {**order.cancellation_policy, "config": {**DEFAULTS, "arrived_penalty_percent": 40}}
        order.save()
        case_no = self.new_case(order)
        quote = self.quote(order, case_no, mode="policy", responsibility="customer",
            occurred_at=(order.arrived_at + timedelta(minutes=1)).isoformat())
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(quote.data["data"]["refund_amount"], 4800, "8000 discounted service, retain agreed 40% plus paid travel")
        self.assertEqual(quote.data["data"]["remaining_amount"], 5200)

    def test_no_show_requires_unanswered_full_wait_and_payment_confirmed(self):
        order = self.make_order(service_started_at=None)
        deadline = order.starts_at + timedelta(minutes=20)
        order.customer_wait_deadline_at = deadline
        order.customer_wait = {"state": "review", "starts_at": order.starts_at.isoformat(),
            "contact_at": (order.arrived_at + timedelta(minutes=1)).isoformat()}
        order.save()
        case_no = self.new_case(order)
        for event, expected in [(deadline - timedelta(seconds=1), 400), (deadline, 200)]:
            quote = self.quote(order, case_no, mode="policy", responsibility="customer", customer_no_show=True, occurred_at=event.isoformat())
            self.assertEqual(quote.status_code, expected, quote.data)
        self.assertEqual(quote.data["data"]["refund_amount"], 5000)
        order.refresh_from_db()
        order.customer_wait = {**order.customer_wait, "responded_at": deadline.isoformat()}
        order.save()
        self.assertEqual(self.quote(order, case_no, mode="policy", responsibility="customer", customer_no_show=True).status_code, 400)

    def test_mixed_wallet_and_external_preview_exactly_match_refund_creation(self):
        from wallets.models import WalletPaymentAllocation
        order = self.make_order()
        WalletPaymentAllocation.objects.create(user=order.customer, business_type="provider_order",
            business_order_no=order.order_no, payable_amount=12000, wallet_amount=5000,
            external_amount=7000, status="consumed")
        case_no = self.new_case(order)
        quote = self.quote(order, case_no)
        self.assertEqual(quote.data["data"]["routes"], {"wallet": 2083, "external": 2917})
        self.assertEqual(self.resolve(case_no, quote).status_code, 200)
        refund = Refund.objects.get()
        self.assertEqual((refund.wallet_refund_amount, refund.external_refund_amount), (2083, 2917))

    def test_existing_cases_and_failed_refunds_cannot_duplicate(self):
        order = self.make_order()
        case_no = self.new_case(order)
        self.assertEqual(self.resolve(case_no, self.quote(order, case_no)).status_code, 200)
        Refund.objects.update(status="failed", failure_reason="模拟待核实结果")
        self.assertEqual(self.intake(order).data["data"]["case_no"], case_no)
        self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(self.quote(order, case_no).status_code, 400)
        self.assertEqual(self.intake(order).data["data"]["fulfillment"]["finance_state"], "refund_attention")

    def test_rejected_dispute_returns_to_review_without_releasing_hold(self):
        order = self.make_order()
        case_no = self.new_case(order)
        result = self.client.post(reverse("backoffice-provider-order-after-sales-action", args=(case_no,)),
            {"action": "reject", "result_note": "售后诉求证据不足，继续核实履约异常"}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.assertEqual(order.status, "pending_confirmation")
        self.assertIsNone(order.confirmation_expires_at)

    def test_todos_move_to_after_sales_then_reconciliation(self):
        order = self.make_order()
        def counts():
            response = self.client.get(reverse("admin-work-summary"))
            self.assertEqual(response.status_code, 200, response.data)
            return {item["key"]: item["count"] for item in response.data["data"]["todos"]}
        self.assertEqual(counts()["fulfillment_review"], 1)
        case_no = self.new_case(order)
        self.assertEqual(counts()["fulfillment_review"], 0)
        self.assertEqual(counts()["provider_after_sales"], 1)
        self.assertEqual(self.resolve(case_no, self.quote(order, case_no, mode="policy", responsibility="customer")).status_code, 200)
        self.assertEqual(counts()["provider_after_sales"], 0)
        self.assertEqual(counts()["termination_reconciliation"], 1)

    def test_full_refund_closes_settlement_only_after_verified_refund(self):
        order = self.make_order(service_started_at=None)
        case_no = self.new_case(order)
        quote = self.quote(order, case_no, component_refunds={"service": 10000, "transport": 2000, "other": 0})
        self.assertEqual(self.resolve(case_no, quote).status_code, 200)
        self.assertFalse(Settlement.objects.filter(status="cancelled").exists())
        refund = Refund.objects.get()
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="TEST", refunded_at=timezone.now())
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(Settlement.objects.get(order=order).status, "cancelled")

    def test_customer_rules_use_original_snapshot_and_event_time(self):
        for number, minutes, expected in [("BUS-EARLY", 20, 3000), ("BUS-LATE", 21, 8000)]:
            order = self.make_order(number, transport_mode="bus")
            case_no = self.new_case(order)
            quote = self.quote(order, case_no, mode="policy", responsibility="customer",
                               occurred_at=(order.departed_at + timedelta(minutes=minutes)).isoformat())
            self.assertEqual(quote.status_code, 200, quote.data)
            self.assertEqual(quote.data["data"]["remaining_amount"], expected)
            self.assertEqual(quote.data["data"]["retained_transport_amount"], 0, "bus compensation includes transport")
            self.assertEqual(quote.data["data"]["policy_version"], "agreed-test-v1")
        order = self.make_order("ARRIVED")
        case_no = self.new_case(order)
        quote = self.quote(order, case_no, mode="policy", responsibility="customer",
                           occurred_at=(order.arrived_at + timedelta(minutes=1)).isoformat())
        self.assertEqual(quote.data["data"]["refund_amount"], 7000)
        self.assertEqual(quote.data["data"]["retained_transport_amount"], 2000)
        self.assertEqual(self.resolve(case_no, quote).status_code, 200)
        refund = Refund.objects.get()
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="TEST", refunded_at=timezone.now())
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled", "later disputed start must not override adjudicated event stage")
        self.assertEqual(Settlement.objects.get(order=order).provider_settlement_amount, 4100)

    def test_zero_refund_does_not_create_fake_refund_or_restore_service(self):
        order = self.make_order()
        case_no = self.new_case(order)
        quote = self.quote(order, case_no, mode="policy", responsibility="customer")
        self.assertEqual(quote.data["data"]["refund_amount"], 0)
        result = self.resolve(case_no, quote)
        self.assertEqual(result.data["data"]["status"], "resolved")
        self.assertFalse(Refund.objects.exists())
        self.assertEqual(Settlement.objects.get(order=order).status, "dispute_frozen")

    def test_missing_original_policy_and_commission_do_not_use_current_rates(self):
        order = self.make_order(cancellation_policy={}, pricing_snapshot={})
        case_no = self.new_case(order)
        self.assertEqual(self.quote(order, case_no, mode="policy", responsibility="customer").status_code, 400)
        self.assertEqual(self.quote(order, case_no).status_code, 400)
        full = self.quote(order, case_no, component_refunds={"service": 10000, "transport": 2000, "other": 0})
        self.assertEqual(full.status_code, 200, full.data)
        self.assertEqual(full.data["data"]["remaining_amount"], 0)

    def test_unverified_wallet_funding_and_changed_payment_are_blocked(self):
        from wallets.models import WalletPaymentAllocation
        order = self.make_order()
        Payment.objects.filter(order=order).update(channel="mixed")
        self.assertEqual(self.intake(order).status_code, 400)
        allocation = WalletPaymentAllocation.objects.create(user=order.customer, business_type="provider_order",
            business_order_no=order.order_no, payable_amount=12000, wallet_amount=5000, external_amount=7000)
        self.assertEqual(self.intake(order).status_code, 400, "held is not proof of consumed funds")
        allocation.status = "consumed"
        allocation.save()
        case_no = self.new_case(order)
        quote = self.quote(order, case_no)
        Payment.objects.filter(order=order).update(payer=self.beijing_user)
        self.assertEqual(self.resolve(case_no, quote).status_code, 400)
        self.assertFalse(Refund.objects.exists())

    def test_distribution_started_after_preview_blocks_refund(self):
        from orders.models import ProviderOrderDistribution
        order = self.make_order()
        case_no = self.new_case(order)
        quote = self.quote(order, case_no)
        settlement = Settlement.objects.create(order=order, provider=order.provider, paid_amount=12000,
            net_service_fee_amount=10000, net_transport_fee_amount=2000, net_other_fee_amount=0,
            platform_commission_rate=30, platform_commission_amount=3000, provider_service_income_amount=7000,
            provider_settlement_amount=9000, frozen_at=timezone.now(), freeze_until=timezone.now())
        ProviderOrderDistribution.objects.create(settlement=settlement, req_date="20261014",
            req_seq_id="SYNTHETIC-ONLY", snapshot={}, payment_fee_amount=42)
        self.assertEqual(self.resolve(case_no, quote).status_code, 400)
        self.assertFalse(Refund.objects.exists())
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)

    def test_manual_customer_adjustment_needs_supervisor_and_token_cannot_be_tampered(self):
        order = self.make_order()
        case_no = self.new_case(order)
        quote = self.quote(order, case_no, responsibility="customer")
        token = quote.data["data"]["token"]
        invalid = self.client.post(reverse("admin-fulfillment-case-resolve", args=(case_no,)),
            {"token": token[:-3] + "bad"}, format="json")
        self.assertEqual(invalid.status_code, 400)
        result = self.resolve(case_no, quote)
        self.assertTrue(result.data["data"]["requires_supervisor"])
        self.assertFalse(Refund.objects.exists())

    def test_permissions_scope_over_refund_stale_preview_and_bypass(self):
        order = self.make_order()
        self.role.permissions.remove("order.after_sales.create")
        self.role.save()
        self.assertEqual(self.intake(order).status_code, 403)
        self.role.permissions.append("order.after_sales.create")
        self.role.save()
        case_no = self.new_case(order)
        self.assertEqual(self.quote(order, case_no, responsibility="provider", mode="policy").status_code, 400)
        self.assertEqual(self.quote(order, case_no, facts_confirmed=False).status_code, 400)
        self.assertEqual(self.quote(order, case_no, component_refunds={"service": 10001, "transport": 0, "other": 0}).status_code, 400)
        self.assertEqual(self.quote(order, case_no, customer_no_show=True, mode="policy", responsibility="customer").status_code, 400)
        bypass = self.client.post(reverse("backoffice-provider-order-after-sales-action", args=(case_no,)),
            {"action": "approve", "approved_amount": 100, "result_note": "试图绕过退款方案预览"}, format="json")
        self.assertEqual(bypass.status_code, 400)
        quote = self.quote(order, case_no)
        self.role.permissions.remove("refund.approve")
        self.role.save()
        self.assertEqual(self.resolve(case_no, quote).status_code, 403)
        self.role.permissions.append("refund.approve")
        self.role.save()
        Order.objects.filter(pk=order.pk).update(fulfillment_revision=2)
        self.assertEqual(self.resolve(case_no, quote).status_code, 400)
        self.assertFalse(Refund.objects.exists())
        Order.objects.filter(pk=order.pk).update(provider=self.beijing)
        self.assertEqual(self.client.get(reverse("admin-fulfillment-case-context", args=(case_no,))).status_code, 404)

    def test_over_limit_escalates_without_refund_and_supervisor_repreviews(self):
        order = self.make_order()
        case_no = self.new_case(order)
        setting = PlatformOperationSetting.current()
        setting.support_refund_single_limit = 100
        setting.save()
        quote = self.quote(order, case_no)
        result = self.resolve(case_no, quote)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data["data"]["requires_supervisor"])
        self.assertEqual(result.data["data"]["status"], "pending")
        self.assertFalse(Refund.objects.exists())
        self.role.permissions.append("refund.supervise")
        self.role.save()
        self.assertEqual(self.resolve(case_no, quote).status_code, 400, "old preview invalidated by escalation")
        self.assertEqual(self.resolve(case_no, self.quote(order, case_no)).status_code, 200)
        self.assertEqual(Refund.objects.count(), 1)
