from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings
from rest_framework.exceptions import ValidationError

from orders import test_wallet_distributions as fixtures
from orders.distributions import query_distribution
from orders.models import ProviderOrderDistribution
from orders.wallet_distributions import execute_wallet_distribution
from providers.models import ProviderIncomeEntry, ProviderIncomeWallet
from providers.test_huifu_user_transport import signed_http_response
from .models import FinanceRecoveryCase
from .recovery import action, create_case


@override_settings(**fixtures.SETTINGS, HUIFU_FINANCE_RECOVERY_ENABLED=True)
class PoolRecoveryTests(TransactionTestCase):
    execute = fixtures.WalletDistributionTests.execute
    receipt = fixtures.WalletDistributionTests.receipt
    respond = fixtures.WalletDistributionTests.respond
    query = fixtures.WalletDistributionTests.query

    def setUp(self):
        fixtures.WalletDistributionTests.setUp(self)
        self.maker = get_user_model().objects.create_user(phone="19900000891")
        self.reviewer = get_user_model().objects.create_user(phone="19900000892")
        self.execute()
        self.record = self.query(trans_stat="F")
        self.pool = self.record.funding_pool
        self.case = create_case(kind="pool", target_id=self.pool.pk, actor=self.maker, note="核查付款暂停")
        self.case = self.act("submit", resolution="resume")

    def act(self, verb, **kwargs):
        self.case.refresh_from_db()
        return action(self.case.pk, actor=self.reviewer if verb == "approve" else self.maker,
                      revision=self.case.revision, action=verb, note="核对原请求、账户资金及凭证",
                      evidence_reference="FIN-OFFLINE-POOL", **kwargs)

    def failed_response(self, url, **kwargs):
        if url.endswith("/acctpayment/pay/query"):
            return signed_http_response(self.receipt(trans_stat="F"))
        return self.respond(url, **kwargs)

    def assert_held(self):
        self.pool.refresh_from_db()
        self.record.refresh_from_db()
        self.assertTrue(self.pool.hold_reason)
        self.assertFalse(self.record.funds_released)
        self.assertIsNone(self.record.failure_reviewed_at)
        self.assertFalse(ProviderIncomeEntry.objects.exists())

    def test_signed_query_and_balance_allow_only_future_orders_not_original_retry(self):
        with patch("requests.sessions.Session.post", side_effect=self.failed_response) as http:
            result = self.act("approve")
        self.assertEqual(result.status, "restored")
        self.assertEqual(http.call_count, 2)
        self.assertTrue(all(not call.args[0].endswith("/acctpayment/pay") for call in http.call_args_list))
        self.pool.refresh_from_db()
        self.record.refresh_from_db()
        self.assertFalse(self.pool.hold_reason)
        self.assertEqual(self.record.status, "failed")
        self.assertTrue(self.record.funds_released)
        self.assertIsNotNone(self.record.failure_reviewed_at)
        self.assertFalse(ProviderIncomeEntry.objects.exists())
        with patch("requests.sessions.Session.post") as no_payment:
            same, created = execute_wallet_distribution(self.order.order_no)
        no_payment.assert_not_called()
        self.assertFalse(created)
        self.assertEqual(same.pk, self.record.pk)
        self.query(trans_stat="F")
        self.pool.refresh_from_db()
        self.assertFalse(self.pool.hold_reason)
        self.query()  # A later contradicting success still re-holds the account.
        self.pool.refresh_from_db()
        self.record.refresh_from_db()
        self.assertTrue(self.pool.hold_reason)
        self.assertEqual(self.record.evidence_conflict_code, "query_terminal_conflict")

    def test_unknown_query_after_historical_failure_must_not_release(self):
        with patch("requests.sessions.Session.post", return_value=signed_http_response(
            self.receipt(trans_stat="P"),
        )):
            with self.assertRaises(ValidationError):
                self.act("approve")
        self.assert_held()
        self.assertEqual(FinanceRecoveryCase.objects.get().status, "review")

    def test_insufficient_platform_funds_keep_reservation(self):
        with patch("requests.sessions.Session.post", side_effect=self.failed_response), patch(
            "orders.balance_payment_gateway.HuifuBalancePaymentGateway.platform_balance",
            return_value={"available_amount": 0, "response_digest": "a" * 64},
        ):
            with self.assertRaises(ValidationError):
                self.act("approve")
        self.assert_held()

    def test_new_pool_generation_during_query_invalidates_approval(self):
        def changed(_):
            self.pool.generation += 1
            self.pool.save()
            return {"available_amount": 100000, "response_digest": "a" * 64}
        with patch("requests.sessions.Session.post", side_effect=self.failed_response), patch(
            "orders.balance_payment_gateway.HuifuBalancePaymentGateway.platform_balance", side_effect=changed,
        ):
            with self.assertRaises(ValidationError):
                self.act("approve")
        self.assert_held()

    def test_new_observation_after_proof_invalidates_release(self):
        def changed(_):
            self.record.observations.create(kind="query", status="unknown")
            return {"available_amount": 100000, "response_digest": "a" * 64}
        with patch("requests.sessions.Session.post", side_effect=self.failed_response), patch(
            "orders.balance_payment_gateway.HuifuBalancePaymentGateway.platform_balance", side_effect=changed,
        ):
            with self.assertRaises(ValidationError):
                self.act("approve")
        self.assert_held()

    def test_superseded_run_token_cannot_restore(self):
        def changed(_):
            FinanceRecoveryCase.objects.filter(pk=self.case.pk).update(run_token=uuid.uuid4())
            return {"available_amount": 100000, "response_digest": "a" * 64}
        with patch("requests.sessions.Session.post", side_effect=self.failed_response), patch(
            "orders.balance_payment_gateway.HuifuBalancePaymentGateway.platform_balance", side_effect=changed,
        ):
            with self.assertRaises(ValidationError):
                self.act("approve")
        self.assert_held()

    def test_conflicting_or_changed_original_record_never_released(self):
        ProviderOrderDistribution.objects.filter(pk=self.record.pk).update(evidence_conflict_code="amount_conflict")
        with patch("requests.sessions.Session.post") as http:
            with self.assertRaises(ValidationError):
                self.act("approve")
        http.assert_not_called()
        self.assert_held()

    def test_disabled_during_review_rejects_without_channel_request(self):
        with override_settings(HUIFU_FINANCE_RECOVERY_ENABLED=False), patch("requests.sessions.Session.post") as http:
            with self.assertRaises(ValidationError):
                self.act("approve")
        http.assert_not_called()
        self.assert_held()

    def test_changed_channel_scope_rejects_without_request(self):
        with override_settings(HUIFU_MERCHANT_ID="9000000000000099"), patch("requests.sessions.Session.post") as http:
            with self.assertRaises(ValidationError):
                self.act("approve")
        http.assert_not_called()
        self.assert_held()

    def test_unexpected_income_credit_blocks_release(self):
        wallet = ProviderIncomeWallet.objects.create(
            provider=self.record.settlement.provider, channel_scope="offline", receiver_id="offline",
            available_amount=7000,
        )
        ProviderIncomeEntry.objects.create(wallet=wallet, distribution=self.record,
            kind="credit", amount=7000, available_delta=7000, reserved_delta=0, source_key="unexpected-offline")
        with patch("requests.sessions.Session.post") as http:
            with self.assertRaises(ValidationError):
                self.act("approve")
        http.assert_not_called()
        self.pool.refresh_from_db()
        self.record.refresh_from_db()
        self.assertTrue(self.pool.hold_reason)
        self.assertFalse(self.record.funds_released)
        self.assertEqual(wallet.available_amount, 7000)

    def test_original_query_remains_idempotent_after_record_only_resolution(self):
        self.act("return")
        self.act("submit", resolution="record")
        self.act("approve")
        self.assert_held()
        with patch("requests.sessions.Session.post", side_effect=self.failed_response):
            query_distribution(self.order.order_no)
        self.assert_held()
