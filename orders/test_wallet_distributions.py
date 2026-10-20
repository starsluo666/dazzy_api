"""Synthetic wallet/mixed settlement lifecycles. HTTP is always mocked."""

from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.serializers import ProviderOrderSettlementSerializer
from providers.models import ProviderIncomeEntry, ProviderIncomeWallet
from providers.tasks import process_income_transfers
from providers.test_huifu_user_transport import signed_http_response
from wallets.models import WalletPaymentAllocation
from . import test_distributions as fixtures
from . import test_wallet_settlement_preparation as preparation_fixtures
from .balance_payment_gateway import HuifuBalancePaymentGateway
from .distribution_preflight import DistributionBlocked
from .distributions import execute_distribution, query_distribution
from .distribution_transport import DistributionUncertain
from .models import (
    ProviderBalancePaymentPool,
    ProviderOrder,
    ProviderOrderDistribution,
    ProviderOrderDistributionPreflight,
    ProviderOrderRefundOrder,
)
from .services import create_provider_order_refund
from .settlement_plans import sync_provider_settlement_plan
from .wallet_distributions import execute_wallet_distribution


SETTINGS = {
    **fixtures.SETTINGS,
    "HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED": True,
    "HUIFU_PROVIDER_BALANCE_PAYMENT_CONFIRMED": True,
    "HUIFU_PROVIDER_BALANCE_PAYMENT_MAX_CENTS": 10000,
    "HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID": "BTEST001",
    "HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_MODE": "request_payer",
    "HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_RESERVE_CENTS": 100,
    "HUIFU_PROVIDER_BALANCE_PAYMENT_TRANSFER_TYPE": "05",
    "HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP": "192.0.2.10",
    "HUIFU_PROVIDER_BALANCE_PAYMENT_START_AT": "2000-01-01T00:00:00+08:00",
}


@override_settings(**SETTINGS)
class WalletDistributionTests(TransactionTestCase):
    def setUp(self):
        fixtures.DistributionStateTests.setUp(self)
        allocation = WalletPaymentAllocation.objects.get(business_order_no=self.order.order_no)
        allocation.wallet_amount, allocation.external_amount = 10000, 0
        allocation.save()
        self.payment.channel, self.payment.delay_acct_flag = "balance", "N"
        self.payment.distribution_cohort = {}
        self.payment.gateway_last_query_status = "S"
        self.payment.gateway_last_query_digest = "a" * 64
        self.payment.save()
        self.recharge, self.debit = preparation_fixtures.WalletSettlementPreparationTests.source(
            self, self.order, 10000, gateway_merchant_id=fixtures.MERCHANT,
        )
        sync_provider_settlement_plan(order_no=self.order.order_no)
        # Any accidentally unmocked call is a test failure, never real traffic.
        guard = patch("requests.sessions.Session.send", side_effect=AssertionError("Real HTTP forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def receipt(self, *, query=True, fee="0.12", **changes):
        record = ProviderOrderDistribution.objects.get(settlement=self.settlement)
        data = {
            "resp_code": "00000000", "huifu_id": fixtures.MERCHANT,
            "out_huifu_id": fixtures.MERCHANT, "ord_amt": "70.00",
            "trans_stat": "S", "delay_acct_flag": "N", "trans_fee_take_flag": "OUT",
            "acct_split_bunch": json.dumps({
                "acct_infos": [{"huifu_id": fixtures.USER_ID, "div_amt": "70.00"}],
                "fee_amt": fee, "fee_huifu_id": fixtures.MERCHANT,
            }),
        }
        if query:
            data.update(org_req_date=record.req_date, org_req_seq_id=record.req_seq_id, org_hf_seq_id="OFFLINE-BALANCE")
        else:
            data.update(req_date=record.req_date, req_seq_id=record.req_seq_id, hf_seq_id="OFFLINE-BALANCE")
        return {**data, **changes}

    def respond(self, url, **kwargs):
        request = kwargs["json"]["data"]
        self.assertFalse(transaction.get_connection().in_atomic_block)
        if url.endswith("/scanpay/query"):
            external = WalletPaymentAllocation.objects.get(business_order_no=self.order.order_no).external_amount
            recharge = request["req_seq_id"] == self.recharge.req_seq_id
            return signed_http_response({
                **request, "resp_code": "00000000", "trans_stat": "S", "delay_acct_flag": "N", "div_flag": "N",
                "acct_id": "BTEST001", "acct_stat": "S",
                "hf_seq_id": self.recharge.gateway_trade_no if recharge else self.payment.gateway_trade_no,
                "trans_amt": "100.00" if recharge else f"{external // 100}.{external % 100:02d}",
            })
        if url.endswith("/balance/query"):
            return signed_http_response({**request, "resp_code": "00000000", "acctInfo_list": json.dumps([{
                "huifu_id": fixtures.MERCHANT, "acct_id": "BTEST001", "acct_type": "01",
                "acct_stat": "N", "avl_bal": "1000.00", "frz_bal": "0.00", "balance_amt": "1000.00",
            }])})
        if url.endswith("/acctpayment/pay"):
            record = ProviderOrderDistribution.objects.get(settlement=self.settlement)
            self.assertEqual(record.status, "submitting")
            self.assertEqual(record.route, "balance")
            self.assertEqual(record.funds_reserved_amount, 7100)
            self.assertEqual(request["ord_amt"], "70.00")
            self.assertEqual(request["out_acct_id"], "BTEST001")
            return signed_http_response(self.receipt(query=False))
        self.assertTrue(url.endswith("/acctpayment/pay/query"))
        return signed_http_response(self.receipt())

    def execute(self):
        with patch("requests.sessions.Session.post", side_effect=self.respond) as http:
            record, created = execute_wallet_distribution(self.order.order_no)
        return record, created, http

    def query(self, **changes):
        with patch("requests.sessions.Session.post", return_value=signed_http_response(self.receipt(**changes))):
            return query_distribution(self.order.order_no)

    def test_durable_registration_then_query_credits_exactly_once_and_releases_slot(self):
        record, created, http = self.execute()
        self.assertTrue(created)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(record.status, "processing")
        self.assertIsNone(record.payment_fee_amount)
        self.assertIsNone(record.account_payment_fee_amount)
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        with patch("requests.sessions.Session.post") as http:
            same, created = execute_wallet_distribution(self.order.order_no)
        http.assert_not_called()
        self.assertEqual(same.pk, record.pk)
        self.assertFalse(created)
        self.query()
        self.query()
        record.refresh_from_db()
        self.assertEqual(record.status, "succeeded")
        self.assertTrue(record.funds_released)
        self.assertEqual(record.account_payment_fee_amount, 12)
        self.assertIsNone(record.split_fee_amount)
        self.assertEqual(ProviderIncomeEntry.objects.count(), 1)
        self.assertEqual(ProviderIncomeWallet.objects.get().available_amount, 7000)
        self.assertEqual(ProviderBalancePaymentPool.objects.get().generation, 2)
        self.assertEqual(record.observations.count(), 3)

    def test_mixed_payment_queries_both_sources_but_pays_provider_only_once(self):
        allocation = WalletPaymentAllocation.objects.get(business_order_no=self.order.order_no)
        allocation.wallet_amount, allocation.external_amount = 3000, 7000
        allocation.save()
        self.debit.amount = 3000
        self.debit.save()
        self.payment.channel = "wechat"
        self.payment.save()
        record, _, http = self.execute()
        self.assertEqual(http.call_count, 4)
        self.assertEqual(record.snapshot["funding_type"], "mixed")
        self.assertEqual(len(record.snapshot["sources"]), 2)
        self.query()
        self.assertEqual(ProviderIncomeWallet.objects.get().available_amount, 7000)

    def test_closed_missing_and_invalid_policy_never_calls_channel(self):
        for key, value in (
            ("ENABLED", False), ("CONFIRMED", False), ("MAX_CENTS", 1),
            ("ACCT_ID", ""), ("FEE_MODE", ""), ("FEE_RESERVE_CENTS", -1),
            ("TRANSFER_TYPE", ""), ("RISK_IP", "127.0.0.1"),
            ("START_AT", ""), ("START_AT", "2026-01-01T00:00:00"),
        ):
            with self.subTest(key=key), override_settings(**{"HUIFU_PROVIDER_BALANCE_PAYMENT_" + key: value}):
                with patch("requests.sessions.Session.post") as http, self.assertRaises((ValidationError, DistributionUncertain)):
                    execute_wallet_distribution(self.order.order_no)
                http.assert_not_called()
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_old_orders_and_untraced_sources_are_not_backpaid(self):
        with override_settings(HUIFU_PROVIDER_BALANCE_PAYMENT_START_AT=timezone.now().isoformat()):
            with patch("requests.sessions.Session.post") as http, self.assertRaises(ValidationError):
                execute_wallet_distribution(self.order.order_no)
            http.assert_not_called()
        self.debit.delete()
        with patch("requests.sessions.Session.post") as http, self.assertRaises(ValidationError):
            execute_wallet_distribution(self.order.order_no)
        http.assert_not_called()

    def test_existing_external_distribution_blocks_second_route(self):
        old = ProviderOrderDistribution.objects.create(
            settlement=self.settlement, req_date="20261001", req_seq_id="OLD-PD",
            snapshot={}, payment_fee_amount=0,
        )
        with patch("requests.sessions.Session.post") as http:
            record, created = execute_wallet_distribution(self.order.order_no)
        self.assertEqual(record.pk, old.pk)
        self.assertFalse(created)
        http.assert_not_called()

    def test_balance_record_also_blocks_original_distribution_and_refund(self):
        self.execute()
        with patch("requests.sessions.Session.post") as http:
            _, created = execute_distribution(self.order.order_no)
            self.assertFalse(created)
            with self.assertRaises(ValidationError):
                self.refund()
        http.assert_not_called()

    def refund(self):
        return create_provider_order_refund(
            order_no=self.order.order_no, amount=100, source_type="admin",
            source_reference="offline", idempotency_key="refund-offline", reason="offline regression",
        )

    def pending_refund(self):
        """Synthetic outstanding record, not a supported post-settlement refund.

        Normal refund creation must stay blocked after settlement. Insert only
        test data to exercise the payout's independent pending-record guard.
        """
        with transaction.atomic():
            ProviderOrder.objects.select_for_update().get(pk=self.order.pk)
            return ProviderOrderRefundOrder.objects.create(
                order=self.order,
                payment_order=self.payment,
                beneficiary=self.customer,
                source_type="admin",
                source_reference="pending-wallet-refund-fixture",
                idempotency_key="pending-wallet-refund-fixture",
                service_fee_refund_amount=100,
                transport_fee_refund_amount=0,
                other_fee_refund_amount=0,
                refund_amount=100,
                wallet_refund_amount=100,
                external_refund_amount=0,
                status=ProviderOrderRefundOrder.Status.PENDING,
                reason="synthetic pending refund regression",
            )

    def test_pending_refund_during_balance_query_prevents_payment(self):
        def balance_after_refund(*_):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.pending_refund()
            return {"available_amount": 100000, "response_digest": "synthetic"}

        with (
            patch.object(
                HuifuBalancePaymentGateway, "platform_balance", side_effect=balance_after_refund,
            ) as balance,
            patch.object(HuifuBalancePaymentGateway, "pay") as pay,
            patch("requests.sessions.Session.post", side_effect=self.respond),
        ):
            with self.assertRaises(DistributionBlocked) as blocked:
                execute_wallet_distribution(self.order.order_no)
        self.assertEqual(blocked.exception.code, "local_conditions")
        balance.assert_called_once()
        pay.assert_not_called()
        refund = ProviderOrderRefundOrder.objects.get(order=self.order)
        self.assertEqual(refund.status, "pending")
        self.assertEqual(refund.wallet_refund_amount, 100)
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get().reason_code, "local_conditions")
        self.assertFalse(ProviderOrderDistribution.objects.exists())
        self.assertFalse(ProviderIncomeEntry.objects.exists())

    def test_insufficient_balance_and_stale_generation_never_register(self):
        with patch("requests.sessions.Session.post", side_effect=self.respond), patch.object(
            HuifuBalancePaymentGateway, "platform_balance", return_value={"available_amount": 7099, "response_digest": "synthetic"},
        ), self.assertRaises(ValidationError):
            execute_wallet_distribution(self.order.order_no)
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get().reason_code, "wallet_funds")
        def changed(*_):
            ProviderBalancePaymentPool.objects.update(generation=100)
            return {"available_amount": 100000, "response_digest": "synthetic"}
        with patch("requests.sessions.Session.post", side_effect=self.respond), patch.object(
            HuifuBalancePaymentGateway, "platform_balance", side_effect=changed,
        ), self.assertRaises(ValidationError):
            execute_wallet_distribution(self.order.order_no)
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_database_rejects_two_pending_payments_in_one_pool(self):
        record, _, _ = self.execute()
        other_order, other_settlement = fixtures.fixtures.ProviderSettlementPlanTests.make_order(self)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProviderOrderDistribution.objects.create(
                settlement=other_settlement, route="balance", req_date=record.req_date,
                req_seq_id="DUPLICATE-POOL", funding_pool=record.funding_pool,
                funds_reserved_amount=1, snapshot={}, payment_fee_amount=None,
            )
        self.assertEqual(ProviderOrderDistribution.objects.count(), 1)

    def second_order(self):
        order, settlement = fixtures.fixtures.ProviderSettlementPlanTests.make_order(
            self, wallet_amount=10000, commission_rate="30.00", service_only=True,
        )
        settlement.status, settlement.freeze_until = "settled", self.now - timedelta(days=1)
        settlement.frozen_at = self.now - timedelta(days=2)
        settlement.save()
        preparation_fixtures.WalletSettlementPreparationTests.source(
            self, order, 10000, gateway_merchant_id=fixtures.MERCHANT,
        )
        sync_provider_settlement_plan(order_no=order.order_no)
        return order

    def test_pending_pool_blocks_next_order_until_verified_success(self):
        self.execute()
        other = self.second_order()
        with patch("requests.sessions.Session.post") as http, self.assertRaises(ValidationError):
            execute_wallet_distribution(other.order_no)
        http.assert_not_called()
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get(settlement__order=other).reason_code, "wallet_busy")
        self.query()
        with patch.object(HuifuBalancePaymentGateway, "verify_source", return_value="synthetic"), patch.object(
            HuifuBalancePaymentGateway, "platform_balance", return_value={"available_amount": 100000, "response_digest": "synthetic"},
        ), patch.object(HuifuBalancePaymentGateway, "pay", return_value={"status": "processing"}) as pay:
            _, created = execute_wallet_distribution(other.order_no)
        self.assertTrue(created)
        pay.assert_called_once()

    def test_pool_hold_blocks_other_orders_even_when_the_old_slot_was_released(self):
        self.execute()
        self.query()
        self.query(fee="0.13")
        other = self.second_order()
        with patch("requests.sessions.Session.post") as http, self.assertRaises(ValidationError):
            execute_wallet_distribution(other.order_no)
        http.assert_not_called()
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get(settlement__order=other).reason_code, "wallet_pool_hold")

    def test_unknown_and_crash_after_registration_never_resubmit(self):
        with patch("requests.sessions.Session.post", side_effect=self.respond), patch.object(HuifuBalancePaymentGateway, "pay", side_effect=TimeoutError()):
            record, _ = execute_wallet_distribution(self.order.order_no)
        self.assertEqual(record.status, "unknown")
        self.query(resp_code="23000001", trans_stat="F")
        with patch("requests.sessions.Session.post") as http:
            _, created = execute_wallet_distribution(self.order.order_no)
        http.assert_not_called()
        self.assertFalse(created)
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        record.refresh_from_db()
        self.assertFalse(record.funds_released)

    def test_verified_failure_holds_account_and_does_not_credit_or_resend(self):
        self.execute()
        record = self.query(trans_stat="F")
        self.assertEqual(record.status, "failed")
        self.assertFalse(record.funds_released)
        self.assertTrue(ProviderBalancePaymentPool.objects.get().hold_reason)
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        self.query()
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        self.assertTrue(ProviderIncomeWallet.objects.get().hold_reason)

    def test_late_conflict_keeps_original_credit_and_holds_both_wallet_and_pool(self):
        self.execute()
        self.query()
        record = self.query(fee="0.13")
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.account_payment_fee_amount, 12)
        self.assertEqual(ProviderIncomeEntry.objects.count(), 1)
        self.assertEqual(ProviderIncomeWallet.objects.get().available_amount, 7000)
        self.assertTrue(ProviderIncomeWallet.objects.get().hold_reason)
        self.assertTrue(ProviderBalancePaymentPool.objects.get().hold_reason)

    def test_fee_above_authorized_budget_never_credits(self):
        self.execute()
        record = self.query(fee="1.01")
        self.assertEqual(record.evidence_conflict_code, "account_payment_fee_limit")
        self.assertFalse(record.funds_released)
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        self.assertTrue(ProviderBalancePaymentPool.objects.get().hold_reason)

    def test_income_failure_rolls_back_success_and_pool_release_then_query_recovers(self):
        self.execute()
        with patch("providers.income_wallet.credit_distribution", side_effect=RuntimeError()), self.assertRaises(RuntimeError):
            self.query()
        record = ProviderOrderDistribution.objects.get()
        self.assertEqual(record.status, "processing")
        self.assertFalse(record.funds_released)
        self.assertEqual(record.observations.count(), 1)
        self.query()
        self.assertEqual(ProviderIncomeEntry.objects.count(), 1)

    def test_queries_continue_when_submission_flag_is_off(self):
        self.execute()
        with override_settings(HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED=False):
            self.assertEqual(self.query().status, "succeeded")

    def test_query_uses_saved_request_not_changed_risk_and_fee_config(self):
        self.execute()
        with override_settings(HUIFU_PROVIDER_BALANCE_PAYMENT_RISK_IP="", HUIFU_PROVIDER_BALANCE_PAYMENT_FEE_MODE="", HUIFU_PROVIDER_BALANCE_PAYMENT_ACCT_ID=""):
            self.assertEqual(self.query().status, "succeeded")

    def test_serializer_distinguishes_route_and_never_exposes_snapshot(self):
        self.execute()
        self.query()
        self.settlement.refresh_from_db()
        data = ProviderOrderSettlementSerializer(self.settlement).data["distribution"]
        self.assertEqual(data["route"], "balance")
        self.assertEqual(data["account_payment_fee_amount"], 12)
        self.assertEqual(data["account_payment_amount"], 7000)
        self.assertIsNone(data["payment_fee_amount"])
        self.assertIsNone(data["split_amount"])
        serialized = json.dumps(data, default=str)
        for secret in (fixtures.MERCHANT, fixtures.USER_ID, "192.0.2.10", "BTEST001"):
            self.assertNotIn(secret, serialized)

    def test_command_requires_confirmation_and_local_modes_never_call_channel(self):
        with self.assertRaises(CommandError), patch("requests.sessions.Session.post") as http:
            call_command("provider_wallet_settlement", "execute", self.order.order_no)
        http.assert_not_called()
        for action in ("inspect", "prepare"):
            with patch("requests.sessions.Session.post") as http:
                call_command("provider_wallet_settlement", action, self.order.order_no, stdout=StringIO())
            http.assert_not_called()

    @override_settings(HUIFU_PROVIDER_INCOME_JOBS_ENABLED=True)
    def test_jobs_dispatch_and_reconcile_balance_without_delayed_confirmation(self):
        with patch("requests.sessions.Session.post", side_effect=self.respond) as http, patch("providers.tasks.refresh_onboarding"):
            result = process_income_transfers()
        self.assertEqual(result["balance_payment_submitted"], 1)
        self.assertEqual(result["distribution_queried"], 1)
        self.assertEqual(result["deferred"], 0)
        self.assertTrue(all("delaytrans" not in call.args[0] for call in http.call_args_list))
        self.assertEqual(ProviderIncomeEntry.objects.count(), 1)

    @override_settings(HUIFU_PROVIDER_INCOME_JOBS_ENABLED=True, HUIFU_PROVIDER_BALANCE_PAYMENT_ENABLED=False)
    def test_jobs_do_not_dispatch_wallet_when_only_original_distribution_is_enabled(self):
        with patch("requests.sessions.Session.post") as http:
            result = process_income_transfers()
        http.assert_not_called()
        self.assertEqual(result["balance_payment_submitted"], 0)

    def test_outer_transaction_rejected(self):
        with transaction.atomic(), self.assertRaises(ValidationError):
            execute_wallet_distribution(self.order.order_no)
