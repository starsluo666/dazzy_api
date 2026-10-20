from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch
import uuid

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from backoffice.serializers import ProviderOrderSettlementSerializer
from wallets.lots import CONSUMPTION_PRICING_VERSION
from wallets.models import (
    UserWallet, WalletBalanceLot, WalletLedgerEntry, WalletLotDebit,
    WalletPaymentAllocation, WalletRechargeOrder,
)
from . import test_settlement_plans as fixtures
from .distributions import _snapshot
from .models import (
    ProviderOrderDistribution, ProviderOrderPaymentOrder,
    ProviderOrderSettlementPlan, ProviderOrderSettlementPlanRevision,
)
from .settlement_plans import sync_provider_settlement_plan
from .services import _complete_provider_order_refund


@override_settings(HUIFU_MERCHANT_ID="test-platform-merchant")
class WalletSettlementPreparationTests(TestCase):
    make_order = fixtures.ProviderSettlementPlanTests.make_order

    @classmethod
    def setUpTestData(cls):
        fixtures.ProviderSettlementPlanTests.setUpTestData.__func__(cls)

    def setUp(self):
        fixtures.ProviderSettlementPlanTests.setUp(self)

    def source(self, order, amount, *, owner=None, **overrides):
        owner = owner or order.customer
        wallet, _ = UserWallet.objects.get_or_create(user=owner)
        values = dict(
            user=owner, unit_face_amount=amount, quantity=1,
            credited_amount=amount, payable_amount=amount,
            discount_rate_bps=9500, discount_amount=0,
            pricing_snapshot={"version": CONSUMPTION_PRICING_VERSION},
            status="paid", paid_at=self.now - timedelta(days=2),
            expires_at=self.now - timedelta(days=1),
            gateway_merchant_id="test-platform-merchant",
            req_date="20261001", req_seq_id=uuid.uuid4().hex,
            gateway_trade_no=uuid.uuid4().hex,
            gateway_last_query_status="S", gateway_last_query_digest="a" * 64,
        )
        values.update(overrides)
        recharge = WalletRechargeOrder.objects.create(**values)
        lot = WalletBalanceLot.objects.create(
            wallet=wallet, recharge_order=recharge, credited_amount=amount,
            available_amount=0, discount_rate_bps=9500,
        )
        debit = WalletLotDebit.objects.create(
            allocation=WalletPaymentAllocation.objects.get(business_order_no=order.order_no),
            lot=lot, amount=amount,
        )
        return recharge, debit

    def prepare(self, order, settlement):
        # Only advance fixture accounting; production preparation never does this.
        settlement.status = "settled"
        settlement.save(update_fields=("status",))
        return sync_provider_settlement_plan(order_no=order.order_no, now=settlement.freeze_until)

    def data(self, plan):
        return plan.funding_snapshot["wallet_settlement_preparation"]

    def codes(self, data):
        return {item["code"] for item in data["source_issues"] + data["business_blockers"]}

    def external_proof(self, order):
        ProviderOrderPaymentOrder.objects.filter(order=order).update(
            gateway_merchant_id="test-platform-merchant",
            gateway_last_query_status="S", gateway_last_query_digest="b" * 64,
        )

    def test_complete_wallet_records_wait_for_channel_and_never_credit_income(self):
        order, settlement = self.make_order(10000)
        self.source(order, 5000)
        self.source(order, 5000)
        plan = self.prepare(order, settlement)
        data = self.data(plan)
        self.assertEqual(data["status"], "awaiting_channel")
        self.assertEqual(data["traceable_wallet_amount"], 10000)
        self.assertEqual(data["untraced_wallet_amount"], 0)
        self.assertEqual((data["provider_receivable_amount"], data["platform_gross_amount"]), (8000, 2000))
        self.assertFalse(data["execution_enabled"])
        self.assertFalse(data["channel_funds_verified"])
        self.assertIsNone(data["channel_transfer_amount"])
        self.assertIsNone(data["channel_fee_amount"])
        self.assertEqual(plan.status, "blocked")
        self.assertEqual(len(data["pending_confirmations"]), 4)
        self.assertFalse(ProviderOrderDistribution.objects.exists())
        self.assertFalse(WalletLedgerEntry.objects.exists())
        from providers.models import ProviderIncomeEntry
        self.assertFalse(ProviderIncomeEntry.objects.exists())

    def test_mixed_sources_kept_separate_without_inventing_transfer_split(self):
        order, settlement = self.make_order(3000)
        self.source(order, 3000)
        self.external_proof(order)
        data = self.data(self.prepare(order, settlement))
        self.assertEqual(data["status"], "awaiting_channel")
        self.assertEqual(data["wallet_remaining_amount"], 3000)
        self.assertEqual(data["external_remaining_amount"], 7000)
        self.assertEqual(data["external_payment"]["paid_amount"], 7000)
        self.assertEqual(data["provider_receivable_amount"], 8000)
        self.assertIsNone(data["channel_transfer_amount"])

    def test_legacy_untraced_balance_requires_review(self):
        order, settlement = self.make_order(10000)
        self.source(order, 3000)
        data = self.data(self.prepare(order, settlement))
        self.assertEqual(data["untraced_wallet_amount"], 7000)
        self.assertIn("untraced_wallet_funds", self.codes(data))
        self.assertEqual(data["status"], "blocked")

    def test_owner_mismatch_does_not_expose_other_users_recharge_reference(self):
        order, settlement = self.make_order(10000)
        recharge, _ = self.source(order, 10000, owner=self.provider_user)
        data = self.data(self.prepare(order, settlement))
        self.assertIn("source_owner_mismatch", self.codes(data))
        self.assertNotIn(recharge.order_no, json.dumps(data))
        self.assertEqual(data["traceable_wallet_amount"], 0)

    def test_recharge_proof_and_merchant_changes_block_preparation(self):
        for changes in (
            {"gateway_merchant_id": "other-platform"},
            {"gateway_last_query_status": "P"},
            {"gateway_last_query_digest": ""},
            {"gateway_trade_no": ""},
            {"status": "pending_payment"},
        ):
            with self.subTest(changes=changes):
                order, settlement = self.make_order(10000)
                self.source(order, 10000, **changes)
                data = self.data(self.prepare(order, settlement))
                self.assertIn("recharge_record_unverified", self.codes(data))
                self.assertEqual(data["untraced_wallet_amount"], 10000)

    def test_overlapping_or_incorrect_lot_amounts_are_not_channel_funds(self):
        order, settlement = self.make_order(10000)
        self.source(order, 8000)
        self.source(order, 8000)
        data = self.data(self.prepare(order, settlement))
        self.assertIn("lot_totals_unverified", self.codes(data))
        self.assertIsNone(data["traceable_wallet_amount"])

    def test_restoration_cannot_exceed_refunded_wallet_amount(self):
        order, settlement = self.make_order(10000)
        _, debit = self.source(order, 10000)
        debit.restored_amount = 1
        debit.save(update_fields=("restored_amount",))
        data = self.data(self.prepare(order, settlement))
        self.assertIn("lot_totals_unverified", self.codes(data))

    def test_mixed_missing_external_proof_or_delayed_funds_remains_blocked(self):
        order, settlement = self.make_order(3000)
        self.source(order, 3000)
        self.assertIn("external_record_unverified", self.codes(self.data(self.prepare(order, settlement))))
        self.external_proof(order)
        ProviderOrderPaymentOrder.objects.filter(order=order).update(delay_acct_flag="Y")
        self.assertIn("external_route_review", self.codes(self.data(self.prepare(order, settlement))))

    def test_missing_payment_and_wrong_allocation_do_not_crash_or_claim_readiness(self):
        order, settlement = self.make_order(3000)
        self.source(order, 3000)
        ProviderOrderPaymentOrder.objects.filter(order=order).delete()
        data = self.data(self.prepare(order, settlement))
        self.assertIn("allocation_mismatch", self.codes(data))
        self.assertIsNone(data["wallet_remaining_amount"])

    def test_repeated_prepare_retains_single_plan_and_unchanged_revisions(self):
        order, settlement = self.make_order(10000)
        recharge, _ = self.source(order, 10000)
        plan = self.prepare(order, settlement)
        original = plan.revisions.get(revision=plan.revision).snapshot
        repeated = self.prepare(order, settlement)
        self.assertEqual((repeated.pk, repeated.revision), (plan.pk, plan.revision))
        recharge.gateway_trade_no = "changed-reference"
        recharge.save(update_fields=("gateway_trade_no",))
        changed = self.prepare(order, settlement)
        self.assertEqual(changed.revision, plan.revision + 1)
        self.assertEqual(plan.revisions.get(revision=plan.revision).snapshot, original)
        self.assertEqual(ProviderOrderSettlementPlan.objects.count(), 1)

    def test_source_list_is_bounded_and_never_claims_complete_totals(self):
        order, settlement = self.make_order(10000)
        self.source(order, 5000)
        self.source(order, 5000)
        with patch("orders.wallet_settlement_preparation.MAX_SOURCE_ROWS", 1):
            data = self.data(self.prepare(order, settlement))
        self.assertTrue(data["sources_truncated"])
        self.assertEqual(len(data["sources"]), 1)
        self.assertIn("source_limit", self.codes(data))
        self.assertIsNone(data["untraced_wallet_amount"])

    def test_business_blocks_and_original_distribution_guard_are_preserved(self):
        order, settlement = self.make_order(10000)
        self.source(order, 10000)
        plan = sync_provider_settlement_plan(order_no=order.order_no, now=self.now)
        self.assertIn("freeze_period", self.codes(self.data(plan)))
        self.prepare(order, settlement)
        with override_settings(HUIFU_PROVIDER_DISTRIBUTION_ENABLED=True), self.assertRaises(ValidationError):
            _snapshot(order, None, now=settlement.freeze_until)
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_original_external_only_plan_is_unchanged(self):
        order, settlement = self.make_order()
        plan = self.prepare(order, settlement)
        self.assertNotIn("wallet_settlement_preparation", plan.funding_snapshot)
        self.assertEqual(plan.status, "ready")

    @override_settings(HUIFU_MERCHANT_ID="")
    def test_missing_merchant_never_marks_sources_complete(self):
        order, settlement = self.make_order(10000)
        self.source(order, 10000)
        data = self.data(self.prepare(order, settlement))
        self.assertIn("merchant_not_configured", self.codes(data))
        self.assertEqual(data["traceable_wallet_amount"], 0)

    def test_other_orders_spending_does_not_change_original_source_snapshot(self):
        order, settlement = self.make_order(3000)
        _, debit = self.source(order, 3000)
        self.external_proof(order)
        plan = self.prepare(order, settlement)
        WalletBalanceLot.objects.filter(pk=debit.lot_id).update(available_amount=100)
        repeated = self.prepare(order, settlement)
        self.assertEqual(repeated.revision, plan.revision)

    def test_allocation_owner_mismatch_is_blocked_without_exposing_recharge(self):
        order, settlement = self.make_order(10000)
        recharge, _ = self.source(order, 10000)
        WalletPaymentAllocation.objects.filter(business_order_no=order.order_no).update(user=self.provider_user)
        data = self.data(self.prepare(order, settlement))
        self.assertIn("allocation_mismatch", self.codes(data))
        self.assertIn("source_owner_mismatch", self.codes(data))
        self.assertNotIn(recharge.order_no, json.dumps(data))

    def test_partial_mixed_refund_tracks_restoration_and_requires_review(self):
        order, settlement = self.make_order(3000)
        _, debit = self.source(order, 3000)
        self.external_proof(order)
        original = self.data(sync_provider_settlement_plan(order_no=order.order_no, now=self.now))
        refund = fixtures.ProviderSettlementPlanTests.refund(self, order, 1001)
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="test-only", refunded_at=self.now)
        plan = sync_provider_settlement_plan(order_no=order.order_no, now=settlement.freeze_until)
        data = self.data(plan)
        self.assertEqual(original["traceable_wallet_amount"], 3000)
        self.assertEqual(data["wallet_remaining_amount"], 2700)
        self.assertEqual(data["external_remaining_amount"], 6299)
        self.assertEqual(data["traceable_wallet_amount"], 2700)
        self.assertEqual(data["untraced_wallet_amount"], 0)
        debit.refresh_from_db()
        self.assertEqual(debit.restored_amount, 300)
        self.assertIn("refunded_order", self.codes(data))
        self.assertFalse(data["execution_enabled"])

    def test_full_wallet_refund_needs_no_settlement(self):
        order, settlement = self.make_order(10000)
        self.source(order, 10000)
        refund = fixtures.ProviderSettlementPlanTests.refund(self, order, 10000)
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="test-only", refunded_at=self.now)
        plan = sync_provider_settlement_plan(order_no=order.order_no, now=settlement.freeze_until)
        data = self.data(plan)
        self.assertEqual(plan.status, "cancelled")
        self.assertEqual(data["status"], "not_required")
        self.assertEqual(data["traceable_wallet_amount"], 0)
        self.assertEqual(data["untraced_wallet_amount"], 0)
        self.assertEqual(data["pending_confirmations"], [])
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_command_inspect_is_read_only_and_prepare_is_local_and_idempotent(self):
        order, settlement = self.make_order(10000)
        self.source(order, 10000)
        before = ProviderOrderSettlementPlanRevision.objects.count()
        call_command("provider_wallet_settlement", "inspect", order.order_no, stdout=StringIO())
        self.assertEqual(before, ProviderOrderSettlementPlanRevision.objects.count())
        output = StringIO()
        call_command("provider_wallet_settlement", "prepare", order.order_no, stdout=output)
        result = json.loads(output.getvalue())
        self.assertFalse(result["execution_enabled"])
        self.assertNotIn("test-platform-merchant", output.getvalue())
        self.assertNotIn(self.customer.phone, output.getvalue())
        before = ProviderOrderSettlementPlanRevision.objects.count()
        call_command("provider_wallet_settlement", "prepare", order.order_no, stdout=StringIO())
        self.assertEqual(before, ProviderOrderSettlementPlanRevision.objects.count())
        with self.assertRaises(CommandError):
            call_command("provider_wallet_settlement", "execute", order.order_no, stdout=StringIO())

    def test_admin_read_does_not_refresh_or_expose_channel_identifiers(self):
        order, settlement = self.make_order(10000)
        self.source(order, 10000)
        self.prepare(order, settlement)
        self.customer.is_superuser = self.customer.is_staff = True
        self.customer.save(update_fields=("is_superuser", "is_staff"))
        client = APIClient()
        client.force_authenticate(self.customer)
        before = ProviderOrderSettlementPlanRevision.objects.count()
        response = client.get(reverse("backoffice-provider-order-finance"), {"record_type": "settlement"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-platform-merchant", json.dumps(response.data, default=str))
        data = response.data["data"]["items"][0]["distribution_plan"]["funding_snapshot"]["wallet_settlement_preparation"]
        self.assertFalse(data["execution_enabled"])
        self.assertEqual(before, ProviderOrderSettlementPlanRevision.objects.count())
        self.assertFalse(ProviderOrderSettlementSerializer(settlement).data["distribution_plan"]["execution_enabled"])
