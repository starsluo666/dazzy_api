"""Signed, synthetic financial fault-injection regressions. Never real funds."""

from datetime import timedelta
from io import StringIO
import json
import uuid
from unittest.mock import patch

from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from backoffice.serializers import ProviderOrderSettlementSerializer
from providers import test_withdrawals as withdrawals
from providers.models import ProviderIncomeWallet, ProviderWithdrawal
from providers.tasks import process_income_transfers
from providers.test_huifu_user_transport import signed_http_response, http_response
from providers.withdrawals import create_withdrawal
from . import test_distributions as fixtures
from .distribution_gateway import HuifuDistributionGateway
from .distribution_preflight import record_preflight_failure
from .distribution_transport import DistributionUncertain
from .distributions import execute_distribution, query_distribution
from .models import ProviderOrderDistribution, ProviderOrderDistributionPreflight
from .services import create_provider_order_refund, _complete_provider_order_refund
from .settlement_plans import sync_provider_settlement_plan


@override_settings(**withdrawals.SETTINGS)
class DistributionSafetyTests(TransactionTestCase):
    setUp = fixtures.DistributionStateTests.setUp
    run_execute = fixtures.DistributionStateTests.run_execute
    use_internal_payment = fixtures.DistributionStateTests.use_internal_payment

    def query(self, data):
        with patch("requests.sessions.Session.post", return_value=signed_http_response(data)):
            return query_distribution(self.order.order_no)

    def credited(self):
        record, _, _ = self.run_execute()
        return self.query(fixtures.confirm_receipt(record, query=True))

    def test_signed_amount_conflict_preserves_credit_freezes_wallet_and_blocks_cash(self):
        record = self.credited()
        data = fixtures.confirm_receipt(record, query=True)
        rows = json.loads(data["acct_split_bunch"])
        rows["acct_infos"][0]["div_amt"] = "69.99"
        data["acct_split_bunch"] = json.dumps(rows)
        record = self.query(data)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.evidence_conflict_code, "query_amount_conflict")
        observation = record.observations.latest("pk")
        self.assertEqual(observation.reason_code, "query_amount_conflict")
        self.assertEqual(len(observation.response_digest), 64)
        self.assertEqual(observation.response_code, "00000000")
        wallet = ProviderIncomeWallet.objects.get(provider=self.provider)
        self.assertTrue(wallet.hold_reason)
        self.assertEqual(wallet.available_amount, 7000)
        self.assertEqual(wallet.entries.count(), 1)
        with patch("requests.sessions.Session.post") as http, self.assertRaises(ValidationError):
            create_withdrawal(self.provider, amount=100, request_key=uuid.uuid4())
        http.assert_not_called()
        self.assertFalse(ProviderWithdrawal.objects.exists())
        # A later valid query cannot silently clear the hold or credit twice.
        self.query(fixtures.confirm_receipt(record, query=True))
        wallet.refresh_from_db()
        self.assertTrue(wallet.hold_reason)
        self.assertEqual(wallet.entries.count(), 1)

    def test_signed_receiver_fee_and_identity_conflicts_are_audited(self):
        record = self.credited()
        for field, value, expected in (
            ("huifu_id", "WRONG", "query_amount_conflict"),
            ("split_fee_huifu_id", fixtures.USER_ID, "query_fee_bearer_conflict"),
        ):
            with self.subTest(field=field):
                data = fixtures.confirm_receipt(record, query=True)
                rows = json.loads(data["acct_split_bunch"])
                rows["acct_infos"][0][field] = value
                data["acct_split_bunch"] = json.dumps(rows)
                result = self.query(data)
                self.assertEqual(result.observations.latest("pk").reason_code, expected)
        data = {**fixtures.confirm_receipt(record, query=True), "org_req_seq_id": "WRONG"}
        self.assertEqual(self.query(data).observations.latest("pk").reason_code, "query_identity_conflict")

    def test_conflict_before_first_credit_creates_hold_and_never_auto_credits(self):
        record, _, _ = self.run_execute()
        data = fixtures.confirm_receipt(record, query=True)
        data["acct_split_bunch"] = json.dumps({"acct_infos": []})
        self.query(data)
        record = self.query(fixtures.confirm_receipt(record, query=True))
        self.assertEqual(record.status, "unknown")
        self.assertTrue(record.evidence_conflict_code)
        wallet = ProviderIncomeWallet.objects.get(provider=self.provider)
        self.assertTrue(wallet.hold_reason)
        self.assertEqual(wallet.available_amount, 0)
        self.assertEqual(wallet.entries.count(), 0)

    def test_missing_optional_fee_cannot_mask_another_rows_amount_conflict(self):
        record = self.credited()
        data = fixtures.confirm_receipt(record, query=True)
        rows = json.loads(data["acct_split_bunch"])
        rows["acct_infos"][0].pop("split_fee_amt")
        rows["acct_infos"][1]["div_amt"] = "29.99"
        data["acct_split_bunch"] = json.dumps(rows)
        record = self.query(data)
        self.assertEqual(record.evidence_conflict_code, "query_amount_conflict")
        self.assertTrue(ProviderIncomeWallet.objects.get().hold_reason)

    def test_timeout_unsigned_or_missing_evidence_does_not_freeze_verified_success(self):
        record = self.credited()
        missing = fixtures.confirm_receipt(record, query=True)
        missing.pop("acct_split_bunch")
        for response in (http_response({"data": missing}), signed_http_response(missing)):
            with patch("requests.sessions.Session.post", return_value=response):
                result = query_distribution(self.order.order_no)
            self.assertEqual(result.status, "succeeded")
            self.assertFalse(result.evidence_conflict_code)
        with patch("requests.sessions.Session.post", side_effect=TimeoutError("SECRET")):
            query_distribution(self.order.order_no)
        wallet = ProviderIncomeWallet.objects.get(provider=self.provider)
        self.assertFalse(wallet.hold_reason)
        self.assertEqual(wallet.entries.count(), 1)

    def test_later_signed_fee_trade_and_terminal_changes_do_not_overwrite_success(self):
        record = self.credited()
        original_trade, original_fee = record.gateway_trade_no, record.split_fee_amount
        cases = (
            (fixtures.confirm_receipt(record, query=True, split_fee_amount="0.11"), "query_fee_conflict"),
            ({**fixtures.confirm_receipt(record, query=True), "hf_seq_id": "CHANGED"}, "query_trade_conflict"),
            ({**fixtures.confirm_receipt(record, query=True), "trans_stat": "F"}, "query_terminal_conflict"),
        )
        for data, code in cases:
            with self.subTest(code=code):
                result = self.query(data)
                self.assertEqual(result.observations.latest("pk").reason_code, code)
                self.assertEqual(result.status, "succeeded")
                self.assertEqual((result.gateway_trade_no, result.split_fee_amount), (original_trade, original_fee))

    def test_preflight_failure_visible_without_changing_local_readiness_or_leaking_exception(self):
        with (
            patch.object(HuifuDistributionGateway, "verify_payment", side_effect=DistributionUncertain("SECRET")),
            patch.object(HuifuDistributionGateway, "confirm") as send,
            self.assertRaises(DistributionUncertain),
        ):
            execute_distribution(self.order.order_no)
        send.assert_not_called()
        self.assertFalse(ProviderOrderDistribution.objects.exists())
        plan = sync_provider_settlement_plan(order_no=self.order.order_no)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, [])
        self.settlement.refresh_from_db()
        data = ProviderOrderSettlementSerializer(self.settlement).data
        self.assertEqual(data["distribution_preflight"]["reason_code"], "payment_proof")
        self.assertIsNone(data["distribution"])
        self.assertNotIn("SECRET", str(data))
        output = StringIO()
        with patch("requests.sessions.Session.post") as http:
            call_command("provider_distribution", "inspect", self.order.order_no, stdout=output)
        http.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["preflight"]["failure_count"], 1)

    def test_platform_fee_shortfall_has_precise_preflight_reason(self):
        self.use_internal_payment()
        with self.assertRaises(DistributionUncertain):
            self.run_execute(fee_amount="30.01")
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get().reason_code, "platform_fee_shortfall")

    def test_preflight_backoff_is_bounded_and_success_wins_over_late_failures(self):
        for count in range(1, 11):
            record_preflight_failure(self.order.order_no, "payment_proof")
            record = ProviderOrderDistributionPreflight.objects.get()
            self.assertEqual(record.failure_count, count)
            self.assertEqual(record.next_retry_at - record.checked_at, timedelta(minutes=min(60, 2 ** (count - 1))))
        self.run_execute()
        record_preflight_failure(self.order.order_no, "payment_proof")
        record.refresh_from_db()
        self.assertEqual(record.status, "registered")
        self.assertFalse(record.reason_code)
        self.assertIsNone(record.next_retry_at)

    def complete_partial_refund(self):
        # Synthetic lifecycle: refund BEFORE local freeze expiry, then settlement.
        self.settlement.status = "risk_frozen"
        self.settlement.save()
        refund, _ = create_provider_order_refund(
            order_no=self.order.order_no, amount=2000, source_type="admin",
            source_reference="offline", idempotency_key="partial-synthetic", reason="offline",
        )
        _complete_provider_order_refund(refund.refund_no, gateway_refund_no="OFFLINE", refunded_at=self.now)
        self.settlement.refresh_from_db()
        self.settlement.status = "settled"
        self.settlement.save()

    def test_completed_partial_refund_stays_locally_ready_but_never_dispatches(self):
        self.complete_partial_refund()
        with patch("requests.sessions.Session.post") as http, self.assertRaisesMessage(ValidationError, "退款后的分账"):
            execute_distribution(self.order.order_no)
        http.assert_not_called()
        plan = sync_provider_settlement_plan(order_no=self.order.order_no)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.refunded_amount, 2000)
        gate = ProviderOrderDistributionPreflight.objects.get()
        self.assertEqual(gate.status, "blocked")
        self.assertEqual(gate.reason_code, "refunded_order")
        self.assertIsNone(gate.next_retry_at)
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_internal_payment_after_partial_refund_is_also_blocked(self):
        self.use_internal_payment()
        self.test_completed_partial_refund_stays_locally_ready_but_never_dispatches()


@override_settings(**{**withdrawals.SETTINGS, "HUIFU_PROVIDER_INCOME_JOBS_ENABLED": True})
class DistributionJobSafetyTests(TransactionTestCase):
    setUp = withdrawals.IncomeJobTests.setUp
    respond = withdrawals.IncomeJobTests.respond

    def test_blocked_refunded_order_is_not_retried_by_background_job(self):
        record_preflight_failure(self.order.order_no, "refunded_order")
        with patch("requests.sessions.Session.post") as http:
            self.assertEqual(process_income_transfers()["distribution_submitted"], 0)
        http.assert_not_called()

    def test_preflight_failure_retries_only_when_due_and_escalates(self):
        with (
            patch("requests.sessions.Session.post", side_effect=self.respond),
            patch.object(HuifuDistributionGateway, "verify_payment", side_effect=DistributionUncertain("SECRET")),
        ):
            self.assertEqual(process_income_transfers()["deferred"], 1)
            with patch("providers.tasks.refresh_onboarding") as refresh:
                self.assertEqual(process_income_transfers()["deferred"], 0)
                refresh.assert_not_called()
            for count in (2, 3):
                ProviderOrderDistributionPreflight.objects.update(next_retry_at=timezone.now() - timedelta(seconds=1))
                if count == 3:
                    with self.assertLogs("orders.distribution_preflight", level="ERROR") as logs:
                        process_income_transfers()
                    self.assertNotIn("SECRET", str(logs.output))
                else:
                    process_income_transfers()
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get().failure_count, 3)
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_receiving_refresh_failure_is_visible_and_backed_off(self):
        with patch("providers.tasks.refresh_onboarding", side_effect=RuntimeError("SECRET")):
            process_income_transfers()
        self.assertEqual(ProviderOrderDistributionPreflight.objects.get().reason_code, "receiving_account")

    def test_recent_success_not_requeried_but_old_success_is_continuously_checked(self):
        with patch("requests.sessions.Session.post", side_effect=self.respond):
            process_income_transfers()
        record = ProviderOrderDistribution.objects.get()
        with patch("requests.sessions.Session.post") as http:
            self.assertEqual(process_income_transfers()["distribution_queried"], 0)
        http.assert_not_called()
        ProviderOrderDistribution.objects.filter(pk=record.pk).update(
            created_at=timezone.now() - timedelta(days=90),
            last_queried_at=timezone.now() - timedelta(hours=7),
        )
        data = fixtures.confirm_receipt(record, query=True)
        rows = json.loads(data["acct_split_bunch"])
        rows["acct_infos"][0]["div_amt"] = "69.99"
        data["acct_split_bunch"] = json.dumps(rows)
        with patch("requests.sessions.Session.post", return_value=signed_http_response(data)) as http:
            self.assertEqual(process_income_transfers()["distribution_queried"], 1)
        http.assert_called_once()
        self.assertTrue(ProviderIncomeWallet.objects.get().hold_reason)
