from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from backoffice.models import (
    AdminRole,
    Organization,
    OrganizationMember,
    ProviderOrderAfterSalesCase,
)
from backoffice.serializers import ProviderOrderSettlementSerializer
from providers.models import ProviderProfile, ProviderService, ServiceCategory
from wallets.models import UserWallet, WalletLedgerEntry, WalletPaymentAllocation

from .models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderSettlement,
    ProviderOrderSettlementPlan,
    ProviderOrderSettlementPlanRevision,
)
from .services import (
    _complete_provider_order_refund,
    advance_provider_order_settlement,
    create_customer_provider_order_after_sales_case,
    create_provider_order_refund,
    ensure_provider_order_settlement,
)
from .settlement_plans import sync_provider_settlement_plan


class ProviderSettlementPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.customer = User.objects.create_user(phone="13900008801")
        cls.provider_user = User.objects.create_user(phone="13900008802")
        cls.provider = ProviderProfile.objects.create(
            user=cls.provider_user,
            display_name="结算测试达人",
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        cls.category = ServiceCategory.objects.create(name="结算测试", slug="settlement-test")
        cls.service = ProviderService.objects.create(
            provider=cls.provider,
            category=cls.category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=8000,
        )

    def setUp(self):
        self.now = timezone.now()
        # These tests must not even construct the network payment gateway.
        gateway_patch = patch("orders.services.get_huifu_payment_gateway")
        self.gateway = gateway_patch.start()
        self.addCleanup(gateway_patch.stop)
        self.addCleanup(self.gateway.assert_not_called)

    def make_order(self, wallet_amount=0, city=None, commission_rate="25.00", service_only=False):
        service_amount = 10000 if service_only else 8000
        transport_amount = 0 if service_only else 1500
        other_amount = 0 if service_only else 500
        order = ProviderOrder.objects.create(
            order_no=f"PLAN{uuid.uuid4().hex[:20]}",
            customer=self.customer,
            provider=self.provider,
            service=self.service,
            provider_name_snapshot="测试达人",
            service_name_snapshot="测试服务",
            billing_type_snapshot="per_session",
            unit_price_amount=service_amount,
            service_fee_amount=service_amount,
            transport_fee_amount=transport_amount,
            other_fee_amount=other_amount,
            payable_amount=10000,
            pricing_snapshot={"platform_commission_rate": commission_rate},
            starts_at=self.now - timedelta(hours=3),
            ends_at=self.now - timedelta(hours=1),
            duration_minutes=120,
            meeting_address="测试地点",
            contact_name="测试用户",
            contact_phone=self.customer.phone,
            status=ProviderOrder.Status.COMPLETED,
            payment_expires_at=self.now - timedelta(hours=4),
            paid_at=self.now - timedelta(hours=5),
            customer_confirmed_at=self.now,
        )
        if city:
            provider_user = User.objects.create_user(phone="13900008809")
            provider = ProviderProfile.objects.create(user=provider_user, service_city_code=city)
            order.provider = provider
            order.save(update_fields=("provider",))
        ProviderOrderPaymentOrder.objects.create(
            order=order,
            payer=self.customer,
            service_fee_amount=service_amount,
            transport_fee_amount=transport_amount,
            other_fee_amount=other_amount,
            discount_amount=0,
            payable_amount=10000,
            pricing_snapshot=order.pricing_snapshot,
            channel="balance" if wallet_amount == 10000 else "wechat",
            status="paid",
            req_date=self.now.strftime("%Y%m%d"),
            req_seq_id=order.order_no,
            gateway_trade_no=f"test-{order.order_no}",
            expires_at=order.payment_expires_at,
            paid_at=order.paid_at,
        )
        WalletPaymentAllocation.objects.create(
            user=self.customer,
            business_type="provider_order",
            business_order_no=order.order_no,
            payable_amount=10000,
            wallet_amount=wallet_amount,
            external_amount=10000 - wallet_amount,
            status="consumed",
        )
        settlement, created = ensure_provider_order_settlement(
            order_no=order.order_no, now=self.now
        )
        self.assertTrue(created)
        return order, settlement

    def plan(self, settlement):
        return ProviderOrderSettlementPlan.objects.get(settlement=settlement)

    def codes(self, plan):
        return {blocker["code"] for blocker in plan.blockers}

    def refund(self, order, amount):
        refund, _ = create_provider_order_refund(
            order_no=order.order_no,
            amount=amount,
            source_type="admin",
            source_reference="test-admin-refund",
            idempotency_key=f"test-refund-{order.order_no}",
            reason="测试退款",
        )
        return refund

    def test_all_funding_modes_get_one_plan_with_original_commission_and_no_money_movement(self):
        for wallet_amount, kind in ((0, "external"), (3000, "mixed"), (10000, "wallet")):
            with self.subTest(kind=kind):
                order, settlement = self.make_order(wallet_amount)
                plan = self.plan(settlement)
                self.assertEqual(plan.funding_type, kind)
                self.assertEqual(plan.status, "waiting")
                self.assertFalse(plan.requires_manual_review)
                self.assertEqual((plan.provider_amount, plan.platform_amount), (8000, 2000))
                self.assertEqual(plan.funding_snapshot["wallet_paid_amount"], wallet_amount)
                self.assertEqual(
                    plan.funding_snapshot["external_paid_amount"], 10000 - wallet_amount
                )
                self.assertFalse(plan.funding_snapshot["channel_funds_verified"])
                self.assertEqual(plan.fee_policy_snapshot["bearer"], "platform")
                self.assertEqual(plan.fee_policy_snapshot["provider_fee_amount"], 0)
                self.assertEqual(plan.fee_policy_snapshot["provider_receivable_amount"], 8000)
                self.assertIsNone(plan.fee_policy_snapshot["total_fee_amount"])
                self.assertIsNone(plan.fee_policy_snapshot["platform_net_amount"])
                self.assertIn("channel_fee_policy_unverified", self.codes(plan))
                self.assertIn("execution_disabled", self.codes(plan))
                self.assertIn("freeze_period", self.codes(plan))
                if wallet_amount:
                    self.assertIn("wallet_route_unconfirmed", self.codes(plan))
                if wallet_amount < 10000:
                    self.assertIn("external_route_unconfirmed", self.codes(plan))
                self.assertEqual(plan.revisions.count(), 1)
                self.assertEqual(order.payment_order.status, "paid")
        self.assertEqual(WalletLedgerEntry.objects.count(), 0)
        self.assertEqual(UserWallet.objects.count(), 0)

    def test_thirty_percent_commission_uses_order_snapshot_and_preserves_fee_policy_on_refund(self):
        order, settlement = self.make_order(commission_rate="30.00", service_only=True)
        original = self.plan(settlement).revisions.get(revision=1).snapshot
        self.assertEqual(
            (settlement.provider_settlement_amount, settlement.platform_commission_amount),
            (7000, 3000),
        )
        self.assertEqual(original["fee_policy_snapshot"]["provider_receivable_amount"], 7000)
        ServiceCategory.objects.filter(pk=self.category.pk).update(platform_commission_rate=90)
        refund = self.refund(order, 2000)
        _complete_provider_order_refund(
            refund.refund_no, gateway_refund_no="test-fee-policy", refunded_at=self.now
        )
        plan = self.plan(settlement)
        self.assertEqual((plan.provider_amount, plan.platform_amount), (5600, 2400))
        self.assertEqual(plan.fee_policy_snapshot["provider_receivable_amount"], 5600)
        self.assertEqual(plan.fee_policy_snapshot["provider_fee_amount"], 0)
        self.assertEqual(plan.fee_policy_snapshot["platform_gross_amount"], 2400)
        # A refund does not prove that the channel refunded its processing fee.
        self.assertIsNone(plan.fee_policy_snapshot["total_fee_amount"])
        self.assertEqual(plan.revisions.get(revision=1).snapshot, original)

    def test_repeated_evaluation_is_idempotent_and_does_not_add_audit_revisions(self):
        order, settlement = self.make_order(3000)
        original = self.plan(settlement)
        for _ in range(3):
            ensure_provider_order_settlement(order_no=order.order_no, now=self.now)
            sync_provider_settlement_plan(order_no=order.order_no, now=self.now)
        current = self.plan(settlement)
        self.assertEqual(current.plan_no, original.plan_no)
        self.assertEqual(current.revision, 1)
        self.assertEqual(current.revisions.count(), 1)
        self.assertEqual(ProviderOrderSettlementPlan.objects.count(), 1)

    @override_settings(HUIFU_PROFIT_SHARING_ENABLED=True)
    def test_freeze_expiry_advances_only_local_accounting_not_funds(self):
        order, settlement = self.make_order()
        self.assertEqual(
            advance_provider_order_settlement(
                order_no=order.order_no,
                now=settlement.freeze_until - timedelta(seconds=1),
            )["state"],
            "not_due",
        )
        self.assertEqual(
            advance_provider_order_settlement(
                order_no=order.order_no,
                now=settlement.freeze_until,
            )["state"],
            "settled",
        )
        plan = self.plan(settlement)
        self.assertEqual(plan.status, "blocked")
        self.assertNotIn("freeze_period", self.codes(plan))
        self.assertIn("execution_disabled", self.codes(plan))
        self.assertFalse(
            ProviderOrderSettlementSerializer(
                ProviderOrderSettlement.objects.get(pk=settlement.pk),
            ).data["distribution_plan"]["execution_enabled"]
        )
        revision = plan.revision
        advance_provider_order_settlement(order_no=order.order_no, now=settlement.freeze_until)
        self.assertEqual(self.plan(settlement).revision, revision)

    def test_missing_allocation_is_unknown_not_inferred_from_wechat_channel(self):
        order, settlement = self.make_order()
        WalletPaymentAllocation.objects.filter(business_order_no=order.order_no).delete()
        plan = sync_provider_settlement_plan(order_no=order.order_no)
        self.assertEqual(plan.funding_type, "unknown")
        self.assertIsNone(plan.funding_snapshot["external_paid_amount"])
        self.assertIn("funding_unverified", self.codes(plan))

    def test_inconsistent_allocation_or_payment_fails_local_reconciliation(self):
        for invalid in ("held", "released", "wrong_user", "wrong_total", "payment_pending"):
            with self.subTest(invalid=invalid):
                order, _ = self.make_order(3000)
                allocation = WalletPaymentAllocation.objects.get(business_order_no=order.order_no)
                if invalid in ("held", "released"):
                    allocation.status = invalid
                elif invalid == "wrong_user":
                    allocation.user = self.provider_user
                elif invalid == "wrong_total":
                    allocation.payable_amount = 9000
                    allocation.external_amount = 6000
                else:
                    ProviderOrderPaymentOrder.objects.filter(order=order).update(
                        status="pending_payment"
                    )
                allocation.save()
                plan = sync_provider_settlement_plan(order_no=order.order_no)
                self.assertFalse(plan.funding_snapshot["locally_reconciled"])
                self.assertEqual(plan.funding_type, "unknown")

    def test_refund_counter_mismatch_is_not_accepted_as_available_funds(self):
        order, _ = self.make_order(3000)
        WalletPaymentAllocation.objects.filter(business_order_no=order.order_no).update(
            wallet_refunded_amount=1,
        )
        plan = sync_provider_settlement_plan(order_no=order.order_no)
        self.assertIn("funding_unverified", self.codes(plan))

    def test_refunds_without_after_sales_block_accounting_in_all_unresolved_states(self):
        for state in ("pending", "processing", "failed"):
            with self.subTest(state=state):
                order, settlement = self.make_order(3000)
                refund = self.refund(order, 1000)
                ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(status=state)
                result = advance_provider_order_settlement(
                    order_no=order.order_no,
                    now=settlement.freeze_until,
                )
                self.assertEqual(result["state"], "dispute_frozen")
                settlement.refresh_from_db()
                self.assertIsNone(settlement.settled_at)
                self.assertIn("refund_unresolved", self.codes(self.plan(settlement)))

    def test_customer_after_sales_immediately_pauses_plan(self):
        order, settlement = self.make_order()
        create_customer_provider_order_after_sales_case(
            order_no=order.order_no,
            customer=self.customer,
            case_type="refund",
            requested_amount=1000,
            reason="测试申请退款",
        )
        self.assertIn("after_sales_open", self.codes(self.plan(settlement)))
        self.assertEqual(self.plan(settlement).status, "waiting")

    def test_all_open_after_sales_states_block_settlement(self):
        for status in ("pending", "processing", "approved"):
            with self.subTest(status=status):
                order, settlement = self.make_order()
                ProviderOrderAfterSalesCase.objects.create(
                    order=order,
                    creator=self.customer,
                    case_type="refund",
                    status=status,
                    original_order_status="completed",
                    requested_amount=1000,
                    reason="测试",
                )
                result = advance_provider_order_settlement(
                    order_no=order.order_no,
                    now=settlement.freeze_until,
                )
                self.assertEqual(result["state"], "dispute_frozen")
                self.assertIn("after_sales_open", self.codes(self.plan(settlement)))

    def test_partial_mixed_refund_reconciles_both_sources_and_keeps_original_audit(self):
        order, settlement = self.make_order(3000)
        original = self.plan(settlement).revisions.get(revision=1).snapshot
        refund = self.refund(order, 1001)
        _complete_provider_order_refund(
            refund.refund_no,
            gateway_refund_no="test-partial-refund",
            refunded_at=self.now,
        )
        plan = self.plan(settlement)
        self.assertEqual(plan.refunded_amount, 1001)
        self.assertEqual(plan.funding_snapshot["wallet_refunded_amount"], 300)
        self.assertEqual(plan.funding_snapshot["external_refunded_amount"], 701)
        self.assertTrue(plan.funding_snapshot["locally_reconciled"])
        self.assertEqual(plan.provider_amount + plan.platform_amount + plan.refunded_amount, 10000)
        self.assertEqual(plan.platform_amount, 1750)
        self.assertNotIn("refund_unresolved", self.codes(plan))
        self.assertEqual(plan.revisions.get(revision=1).snapshot, original)
        revision = plan.revision
        _complete_provider_order_refund(
            refund.refund_no,
            gateway_refund_no="test-partial-refund",
            refunded_at=self.now,
        )
        self.assertEqual(self.plan(settlement).revision, revision)

    def test_full_refund_cancels_plan_without_a_transfer(self):
        order, settlement = self.make_order(3000)
        refund = self.refund(order, 10000)
        _complete_provider_order_refund(
            refund.refund_no,
            gateway_refund_no="test-full-refund",
            refunded_at=self.now,
        )
        plan = self.plan(settlement)
        self.assertEqual(plan.status, "cancelled")
        self.assertEqual((plan.provider_amount, plan.platform_amount), (0, 0))
        self.assertEqual(plan.blockers, [])
        self.assertEqual(plan.refunded_amount, 10000)
        self.assertIsNone(plan.fee_policy_snapshot["total_fee_amount"])
        self.assertIsNone(plan.fee_policy_snapshot["platform_net_amount"])

    def test_commission_configuration_changes_do_not_reprice_existing_orders(self):
        order, settlement = self.make_order()
        ServiceCategory.objects.filter(pk=self.category.pk).update(platform_commission_rate=90)
        advance_provider_order_settlement(order_no=order.order_no, now=settlement.freeze_until)
        self.assertEqual(self.plan(settlement).platform_amount, 2000)
        settlement.refresh_from_db()
        self.assertEqual(settlement.platform_commission_rate, Decimal("25.00"))

    def test_missing_completion_is_not_eligible(self):
        order, _ = self.make_order()
        ProviderOrder.objects.filter(pk=order.pk).update(customer_confirmed_at=None)
        plan = sync_provider_settlement_plan(order_no=order.order_no)
        self.assertIn("order_not_completed", self.codes(plan))

    def test_legacy_settlement_requires_review_and_refresh_cannot_clear_it(self):
        order, settlement = self.make_order()
        # Emulate a pre-migration settlement with no distribution preparation rows.
        ProviderOrderSettlementPlanRevision.objects.all().delete()
        ProviderOrderSettlementPlan.objects.all().delete()
        ProviderOrderSettlement.objects.filter(pk=settlement.pk).update(
            status="settled",
            settled_at=self.now,
        )
        settlement.refresh_from_db()
        self.assertIsNone(ProviderOrderSettlementSerializer(settlement).data["distribution_plan"])
        plan = sync_provider_settlement_plan(order_no=order.order_no, now=settlement.freeze_until)
        self.assertTrue(plan.requires_manual_review)
        self.assertIn("historical_review", self.codes(plan))
        repeat = sync_provider_settlement_plan(
            order_no=order.order_no,
            now=settlement.freeze_until,
            new_settlement=True,
        )
        self.assertTrue(repeat.requires_manual_review)
        self.assertEqual(repeat.plan_no, plan.plan_no)

    def test_unique_plan_and_conservation_constraints(self):
        _, settlement = self.make_order()
        plan = self.plan(settlement)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderOrderSettlementPlan.objects.filter(pk=plan.pk).update(provider_amount=9999)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderOrderSettlementPlan.objects.create(
                settlement=settlement,
                paid_amount=10000,
                refunded_amount=0,
                provider_amount=8000,
                platform_amount=2000,
                evaluated_at=self.now,
            )

    def test_finance_api_exposes_read_only_plans_with_city_scope(self):
        _, handan = self.make_order()
        self.make_order(city="110100")
        staff = User.objects.create_user(phone="13900008803")
        org = Organization.objects.create(
            name="测试运营",
            code="plan-test",
            organization_type=Organization.Type.CITY_AGENT,
            city_codes=["130400"],
        )
        role = AdminRole.objects.create(
            organization=org,
            name="财务只读",
            code="plan-read",
            permissions=["order.finance.view"],
            data_scope=AdminRole.DataScope.CITY,
        )
        OrganizationMember.objects.create(user=staff, organization=org, role=role)
        client = APIClient()
        client.force_authenticate(staff)
        before = ProviderOrderSettlementPlanRevision.objects.count()
        response = client.get(
            reverse("backoffice-provider-order-finance"), {"record_type": "settlement"}
        )
        self.assertEqual(response.status_code, 200)
        items = response.data["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["settlement_no"], handan.settlement_no)
        self.assertFalse(items[0]["distribution_plan"]["execution_enabled"])
        self.assertEqual(items[0]["distribution_plan"]["fee_policy_snapshot"]["bearer"], "platform")
        self.assertIsNone(
            items[0]["distribution_plan"]["fee_policy_snapshot"]["platform_net_amount"]
        )
        self.assertEqual(ProviderOrderSettlementPlanRevision.objects.count(), before)
        client.force_authenticate(self.customer)
        self.assertEqual(client.get(reverse("backoffice-provider-order-finance")).status_code, 403)

    def test_audit_failure_rolls_back_settlement_and_plan_together(self):
        order, settlement = self.make_order()
        with (
            patch(
                "orders.settlement_plans.ProviderOrderSettlementPlanRevision.objects.create",
                side_effect=RuntimeError("test audit storage failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            advance_provider_order_settlement(order_no=order.order_no, now=settlement.freeze_until)
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, "risk_frozen")
        self.assertIsNone(settlement.settled_at)
        self.assertEqual(self.plan(settlement).revision, 1)
