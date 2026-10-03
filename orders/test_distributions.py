"""Offline pilot tests. Never call a real payment or use a business database."""

import copy
from datetime import timedelta
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from providers.models import ProviderReceivingAccount, ProviderReceivingAttempt
from providers.test_huifu_user_transport import CHANNEL_KEY, signed_http_response, http_response
from providers.test_receiving_onboarding import CHANNEL_SETTINGS, USER_ID, CASH
from providers.receiving_onboarding import ONBOARDING_CONSENT_VERSION
from .distribution_gateway import HuifuDistributionGateway, money, receivers
from .distribution_transport import DistributionUncertain
from .distributions import execute_distribution, query_distribution, payment_cohort, _save_result
from .huifu import HuifuPaymentConfig, HuifuAggregatePaymentGateway, _canonical_digest
from .models import ProviderOrderDistribution, ProviderOrderPaymentOrder, ProviderOrderRefundOrder
from .services import process_provider_order_refund, create_provider_order_refund
from . import test_settlement_plans as fixtures


MERCHANT = "9000000000000004"
SETTINGS = {
    **CHANNEL_SETTINGS,
    "HUIFU_PAYMENT_ENABLED": True,
    "HUIFU_RSA_PUBLIC_KEY": CHANNEL_KEY.public_key().export_key().decode(),
    "HUIFU_MERCHANT_ID": MERCHANT,
    "HUIFU_NOTIFY_URL": "https://example.invalid/notify/",
    "HUIFU_FEE_FLAG": "1",
    "HUIFU_SKILL_SOURCE": "hfps/1.3.5",
    "WECHAT_OFFICIAL_ACCOUNT_APP_ID": "wx-offline",
    "HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED": True,
    "HUIFU_PROVIDER_DISTRIBUTION_ENABLED": True,
    "HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED": True,
    "HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS": 10000,
}


def snapshot():
    return {
        "merchant_id": MERCHANT,
        "paid_amount": 10000,
        "provider_amount": 7000,
        "platform_amount": 3000,
        "payment": {"req_date": "20261003", "req_seq_id": "PAY1", "gateway_trade_no": "PAY-HF1"},
        "receivers": receivers(USER_ID, MERCHANT, 7000, 3000),
    }


def payment_receipt(snap):
    return {
        "resp_code": "00000000",
        "req_date": snap["payment"]["req_date"],
        "req_seq_id": snap["payment"]["req_seq_id"],
        "huifu_id": MERCHANT,
        "hf_seq_id": snap["payment"]["gateway_trade_no"],
        "trans_stat": "S",
        "delay_acct_flag": "Y",
        "trans_amt": "100.00",
        "unconfirm_amt": "100.00",
        "payment_fee": json.dumps(
            {"fee_huifu_id": MERCHANT, "fee_flag": "1", "fee_amount": "0.60"}
        ),
    }


def confirm_receipt(record, *, query=False):
    common = {
        "resp_code": "00000000",
        "huifu_id": MERCHANT,
        "hf_seq_id": "SPLIT-HF1",
        "trans_stat": "S",
        "acct_split_bunch": json.dumps(
            {
                "acct_infos": [
                    {**row, "split_fee_amt": "0.10", "split_fee_huifu_id": MERCHANT}
                    for row in record.snapshot["receivers"]
                ]
            }
        ),
    }
    if query:
        return {**common, "org_req_date": record.req_date, "org_req_seq_id": record.req_seq_id}
    return {
        **common,
        "req_date": record.req_date,
        "req_seq_id": record.req_seq_id,
        "org_req_date": record.snapshot["payment"]["req_date"],
        "org_req_seq_id": record.snapshot["payment"]["req_seq_id"],
    }


@override_settings(**SETTINGS)
class DistributionContractTests(SimpleTestCase):
    def setUp(self):
        self.gateway = HuifuDistributionGateway(HuifuPaymentConfig.from_settings())
        self.record = SimpleNamespace(req_date="20261004", req_seq_id="PD1", snapshot=snapshot())

    def test_real_sdk_routes_signed_envelopes_and_request_identities(self):
        responses = [
            payment_receipt(self.record.snapshot),
            confirm_receipt(self.record),
            confirm_receipt(self.record, query=True),
        ]
        from dg_sdk.core.api_request import ApiRequest

        original = ApiRequest.__dict__["_build_return_data"]
        with patch(
            "requests.sessions.Session.post",
            side_effect=[signed_http_response(item) for item in responses],
        ) as http:
            self.assertEqual(
                self.gateway.verify_payment(self.record.snapshot)["payment_fee_amount"], 60
            )
            self.assertEqual(self.gateway.confirm(self.record)["status"], "processing")
            result = self.gateway.query(self.record)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["split_fee_amount"], 20)
        calls = http.call_args_list
        self.assertEqual(
            [call.args[0] for call in calls],
            [
                "https://api.huifu.com" + path
                for path in (
                    "/v4/trade/payment/scanpay/query",
                    "/v2/trade/payment/delaytrans/confirm",
                    "/v3/trade/payment/delaytrans/confirmquery",
                )
            ],
        )
        self.assertEqual(calls[2].kwargs["json"]["data"]["org_req_seq_id"], "PD1")
        self.assertEqual(calls[1].kwargs["json"]["data"]["org_req_seq_id"], "PAY1")
        self.assertEqual(
            json.loads(calls[1].kwargs["json"]["data"]["acct_split_bunch"])["acct_infos"],
            snapshot()["receivers"],
        )
        self.assertEqual(calls[1].kwargs["headers"]["jpt-x-skill-source"], "hfps/1.3.5")
        self.assertIs(ApiRequest.__dict__["_build_return_data"], original)

    def test_unsigned_bad_signature_and_sdk_bypass_are_rejected(self):
        data = confirm_receipt(self.record, query=True)
        tampered = signed_http_response(data)
        envelope = json.loads(tampered.text)
        envelope["data"]["trans_stat"] = "F"
        for response in (
            http_response(data),
            http_response({"data": data}),
            http_response(envelope),
        ):
            with (
                self.subTest(response=response),
                patch("requests.sessions.Session.post", return_value=response),
                self.assertRaises(DistributionUncertain),
            ):
                self.gateway.query(self.record)
        with (
            patch("dg_sdk.V3TradePaymentDelaytransConfirmqueryRequest.post", return_value=data),
            self.assertRaises(DistributionUncertain),
        ):
            self.gateway.query(self.record)

    def test_payment_proof_checks_all_identifiers_delay_funds_and_platform_fees(self):
        for key, value in (
            ("req_seq_id", "wrong"),
            ("huifu_id", USER_ID),
            ("hf_seq_id", "wrong"),
            ("delay_acct_flag", "N"),
            ("trans_stat", "P"),
            ("trans_amt", "99.99"),
            ("unconfirm_amt", "99.40"),
            ("payment_fee", "{}"),
            (
                "payment_fee",
                json.dumps({"fee_amount": "0.60", "fee_flag": "1", "fee_huifu_id": USER_ID}),
            ),
        ):
            data = {**payment_receipt(snapshot()), key: value}
            with (
                self.subTest(key=key, value=value),
                patch("requests.sessions.Session.post", return_value=signed_http_response(data)),
                self.assertRaises(DistributionUncertain),
            ):
                self.gateway.verify_payment(snapshot())

    def test_query_requires_exact_receivers_amounts_fee_evidence_and_identity(self):
        valid = confirm_receipt(self.record, query=True)
        variants = [
            {**valid, "org_req_seq_id": "PAY1"},
            {**valid, "huifu_id": USER_ID},
            {**valid, "hf_seq_id": ""},
            {**valid, "acct_split_bunch": "[]"},
        ]
        for key, value in (
            ("div_amt", "69.99"),
            ("split_fee_huifu_id", USER_ID),
            ("huifu_id", "wrong"),
            ("split_fee_amt", None),
        ):
            rows = json.loads(valid["acct_split_bunch"])
            rows["acct_infos"][0][key] = value
            variants.append({**valid, "acct_split_bunch": json.dumps(rows)})
        rows = json.loads(valid["acct_split_bunch"])
        rows["acct_infos"][1] = copy.deepcopy(rows["acct_infos"][0])
        variants.append({**valid, "acct_split_bunch": json.dumps(rows)})
        for data in variants:
            with (
                self.subTest(data=data),
                patch("requests.sessions.Session.post", return_value=signed_http_response(data)),
                self.assertRaises(DistributionUncertain),
            ):
                self.gateway.query(self.record)

    def test_not_found_is_unknown_and_p_f_are_not_success(self):
        for changes, expected in (
            ({"resp_code": "23000001"}, "unknown"),
            ({"trans_stat": "P"}, "processing"),
            ({"trans_stat": "F"}, "failed"),
            ({"trans_stat": ""}, "unknown"),
        ):
            data = {**confirm_receipt(self.record, query=True), **changes}
            with patch("requests.sessions.Session.post", return_value=signed_http_response(data)):
                self.assertEqual(self.gateway.query(self.record)["status"], expected)

    def test_money_rejects_float_negative_exponent_and_zero_receivers_omitted(self):
        for value in (0.1, "1", "-1.00", "1e2", "NaN", "0.001", True):
            with self.subTest(value=value), self.assertRaises(DistributionUncertain):
                money(value)
        self.assertEqual(money("0.01"), 1)
        self.assertEqual(
            receivers(USER_ID, MERCHANT, 1, 0), [{"huifu_id": USER_ID, "div_amt": "0.01"}]
        )

    def test_payment_delay_default_n_and_explicit_y(self):
        gateway = HuifuAggregatePaymentGateway(self.gateway.config)
        response = {
            "req_date": "20261003",
            "req_seq_id": "PAY1",
            "huifu_id": MERCHANT,
            "resp_code": "00000000",
            "trans_stat": "P",
            "pay_info": "{}",
        }
        for flag in (None, "Y"):
            with patch("dg_sdk.Payment.create", return_value=response) as call:
                gateway.create_payment(
                    req_date="20261003",
                    req_seq_id="PAY1",
                    amount=100,
                    goods_desc="test",
                    trade_type="T_JSAPI",
                    attach="test",
                    time_expire="20261003120000",
                    sub_openid="offline",
                    **({"delay_acct_flag": flag} if flag else {}),
                )
            self.assertEqual(call.call_args.args[0].delay_acct_flag, flag or "N")


@override_settings(**SETTINGS)
class DistributionStateTests(TransactionTestCase):
    def setUp(self):
        fixtures.ProviderSettlementPlanTests.setUpTestData.__func__(type(self))
        self.provider.status = "approved"
        self.provider.identity_status = "verified"
        self.provider.save()
        self.now = timezone.now()
        self.order, self.settlement = fixtures.ProviderSettlementPlanTests.make_order(
            self, commission_rate="30.00", service_only=True
        )
        self.settlement.frozen_at = self.now - timedelta(days=2)
        self.settlement.freeze_until = self.now - timedelta(days=1)
        self.settlement.status = "settled"
        self.settlement.save()
        self.config = HuifuPaymentConfig.from_settings()
        allowlist = override_settings(HUIFU_PROVIDER_DISTRIBUTION_IDS=(str(self.provider.pk),))
        allowlist.enable()
        self.addCleanup(allowlist.disable)
        self.account = ProviderReceivingAccount.objects.create(
            provider=self.provider,
            details_ciphertext="unused",
            consented_at=self.now,
            onboarding_consented_at=self.now,
            user_huifu_id=USER_ID,
            channel_status="active",
            card_status="S",
            settlement_status="S",
            cash_status="S", automatic_settlement_disabled=True, verified_cash_config=CASH,
            cash_card_ciphertext="unused", onboarding_consent_version=ONBOARDING_CONSENT_VERSION,
            audit_status="Y",
            channel_scope=_canonical_digest(
                [
                    "prod",
                    self.config.sys_id,
                    self.config.product_id,
                    SETTINGS["HUIFU_USER_UPPER_ID"],
                ]
            ),
            channel_checked_at=self.now,
        )
        ProviderReceivingAttempt.objects.create(
            account=self.account,
            kind="configure",
            req_date="20261003",
            req_seq_id="OPEN1",
            status="succeeded",
            settlement_config={"out_settle_flag": "1", "out_settle_huifuid": MERCHANT},
        )
        self.allocation = SimpleNamespace(wallet_amount=0, external_amount=10000)
        self.payment = ProviderOrderPaymentOrder.objects.get(order=self.order)
        self.payment.gateway_merchant_id = MERCHANT
        self.payment.delay_acct_flag = "Y"
        self.payment.distribution_cohort = payment_cohort(
            self.order, self.allocation, self.config, now=self.now
        )
        self.payment.save()

    def run_execute(self):
        def respond(url, **kwargs):
            if url.endswith("/scanpay/query"):
                snap = {
                    **snapshot(),
                    "payment": {
                        "req_date": self.payment.req_date,
                        "req_seq_id": self.payment.req_seq_id,
                        "gateway_trade_no": self.payment.gateway_trade_no,
                    },
                }
                return signed_http_response(payment_receipt(snap))
            record = ProviderOrderDistribution.objects.get(settlement=self.settlement)
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertEqual(record.status, "submitting")
            return signed_http_response(confirm_receipt(record))

        with patch("requests.sessions.Session.post", side_effect=respond) as http:
            record, created = execute_distribution(self.order.order_no)
        return record, created, http

    def test_execute_persists_first_and_duplicate_never_sends_again(self):
        record, created, http = self.run_execute()
        self.assertTrue(created)
        self.assertEqual(http.call_count, 2)
        self.assertEqual(record.status, "processing")
        self.assertEqual(record.snapshot["provider_amount"], 7000)
        self.assertEqual(record.snapshot["platform_amount"], 3000)
        with patch("requests.sessions.Session.post") as http:
            repeated, created = execute_distribution(self.order.order_no)
        self.assertFalse(created)
        self.assertEqual(repeated.pk, record.pk)
        http.assert_not_called()

    def test_query_success_preserves_provider_share_and_unknown_bank_fees(self):
        record, _, _ = self.run_execute()
        with patch(
            "requests.sessions.Session.post",
            return_value=signed_http_response(confirm_receipt(record, query=True)),
        ):
            record = query_distribution(self.order.order_no)
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.payment_fee_amount, 60)
        self.assertEqual(record.split_fee_amount, 20)
        self.assertEqual(record.observations.count(), 2)
        from backoffice.serializers import ProviderOrderSettlementSerializer

        self.settlement.refresh_from_db()
        data = ProviderOrderSettlementSerializer(self.settlement).data["distribution"]
        self.assertEqual(data["provider_amount"], 7000)
        self.assertFalse(data["bank_arrival_verified"])
        self.assertIsNone(data["bank_settlement_fee_amount"])
        self.assertNotIn(USER_ID, str(data))

    def test_timeout_and_crash_are_query_only(self):
        with patch.object(
            HuifuDistributionGateway, "confirm", side_effect=TimeoutError("do-not-log-secret")
        ):
            record, _, _ = self.run_execute()
        self.assertEqual(record.status, "unknown")
        self.assertNotIn("do-not-log-secret", record.attention_reason)
        record.status = "submitting"  # crash after durable request registration
        record.save()
        with patch("requests.sessions.Session.post") as http:
            repeated, created = execute_distribution(self.order.order_no)
        self.assertFalse(created)
        self.assertEqual(repeated.req_seq_id, record.req_seq_id)
        http.assert_not_called()

    def test_query_still_allowed_when_execution_disabled_and_never_regresses_terminal(self):
        record, _, _ = self.run_execute()
        with (
            override_settings(HUIFU_PROVIDER_DISTRIBUTION_ENABLED=False),
            patch(
                "requests.sessions.Session.post",
                return_value=signed_http_response(confirm_receipt(record, query=True)),
            ),
        ):
            result = query_distribution(self.order.order_no)
        self.assertEqual(result.status, "succeeded")
        _save_result(record.pk, {"status": "processing"}, queried=False)
        record.refresh_from_db()
        self.assertEqual(record.status, "succeeded")
        self.assertFalse(record.attention_reason)  # A late submit ACK is not a conflicting query.

    def test_closed_scope_flags_and_limits_never_call_network(self):
        for changes in (
            {"HUIFU_PROVIDER_DISTRIBUTION_ENABLED": False},
            {"HUIFU_PROVIDER_DISTRIBUTION_IDS": ()},
            {"HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED": False},
            {"HUIFU_PROVIDER_DISTRIBUTION_MAX_CENTS": 0},
            {"HUIFU_FEE_FLAG": "2"},
            {"HUIFU_MERCHANT_ID": "wrong"},
        ):
            with (
                self.subTest(changes=changes),
                override_settings(**changes),
                patch("requests.sessions.Session.post") as http,
                self.assertRaises(ValidationError),
            ):
                execute_distribution(self.order.order_no)
            http.assert_not_called()
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_wallet_mixed_and_default_closed_payment_do_not_select_delay(self):
        for allocation in (
            SimpleNamespace(wallet_amount=1, external_amount=9999),
            SimpleNamespace(wallet_amount=10000, external_amount=0),
        ):
            self.assertEqual(payment_cohort(self.order, allocation, self.config, now=self.now), {})
        with override_settings(HUIFU_PROVIDER_DELAYED_PAYMENT_ENABLED=False):
            self.assertEqual(
                payment_cohort(self.order, self.allocation, self.config, now=self.now), {}
            )

    def test_legacy_payment_and_stale_receiver_block_before_network(self):
        for model, field, value in (
            (self.payment, "delay_acct_flag", "N"),
            (self.payment, "distribution_cohort", {}),
            (self.account, "channel_checked_at", self.now - timedelta(hours=1)),
            (self.account, "channel_scope", "wrong"),
        ):
            original = getattr(model, field)
            setattr(model, field, value)
            model.save()
            with (
                self.subTest(field=field),
                patch("requests.sessions.Session.post") as http,
                self.assertRaises(ValidationError),
            ):
                execute_distribution(self.order.order_no)
            http.assert_not_called()
            setattr(model, field, original)
            model.save()

    def test_conditions_changed_during_preflight_cannot_dispatch(self):
        def preflight(snap):
            self.settlement.status = "dispute_frozen"
            self.settlement.save()
            return {"payment_fee_amount": 60, "payment_query_digest": "offline"}

        with (
            patch.object(HuifuDistributionGateway, "verify_payment", side_effect=preflight),
            patch.object(HuifuDistributionGateway, "confirm") as send,
            self.assertRaises(ValidationError),
        ):
            execute_distribution(self.order.order_no)
        send.assert_not_called()
        self.assertFalse(ProviderOrderDistribution.objects.exists())

    def test_historical_and_unresolved_refund_records_never_dispatch(self):
        from .models import ProviderOrderSettlementPlan

        plan = ProviderOrderSettlementPlan.objects.get(settlement=self.settlement)
        plan.requires_manual_review = True
        plan.save()
        with patch("requests.sessions.Session.post") as http, self.assertRaises(ValidationError):
            execute_distribution(self.order.order_no)
        http.assert_not_called()
        plan.requires_manual_review = False
        plan.save()
        refund = ProviderOrderRefundOrder.objects.create(
            order=self.order,
            payment_order=self.payment,
            beneficiary=self.customer,
            source_type="admin",
            source_reference="offline",
            idempotency_key="unresolved",
            service_fee_refund_amount=1,
            transport_fee_refund_amount=0,
            other_fee_refund_amount=0,
            refund_amount=1,
            external_refund_amount=1,
            reason="offline",
            status="failed",
        )
        for state in ("pending", "processing", "failed"):
            refund.status = state
            refund.save()
            with (
                self.subTest(state=state),
                patch("requests.sessions.Session.post") as http,
                self.assertRaises(ValidationError),
            ):
                execute_distribution(self.order.order_no)
            http.assert_not_called()

    def test_bank_fee_bearer_must_be_verified_and_no_atomic_outer_transaction(self):
        self.account.verified_cash_config = {**CASH, "out_fee_flag": "2"}
        self.account.save()
        with (
            patch("requests.sessions.Session.post") as http,
            self.assertRaisesMessage(ValidationError, "平台承担"),
        ):
            execute_distribution(self.order.order_no)
        http.assert_not_called()
        with transaction.atomic(), self.assertRaisesMessage(ValidationError, "独立事务"):
            execute_distribution(self.order.order_no)

    def test_query_different_channel_scope_never_calls_network(self):
        self.run_execute()
        with (
            override_settings(HUIFU_PRODUCT_ID="OTHER"),
            patch("requests.sessions.Session.post") as http,
            self.assertRaises(ValidationError),
        ):
            query_distribution(self.order.order_no)
        http.assert_not_called()

    def test_refund_creation_and_dispatch_both_block_after_distribution(self):
        record, _, _ = self.run_execute()
        for status in ("submitting", "unknown", "processing", "succeeded", "failed"):
            record.status = status
            record.save()
            with self.subTest(status=status), self.assertRaisesMessage(ValidationError, "分账回退"):
                create_provider_order_refund(
                    order_no=self.order.order_no,
                    amount=1,
                    source_type="admin",
                    source_reference="offline",
                    idempotency_key="test-refund",
                    reason="test",
                )
        refund = ProviderOrderRefundOrder.objects.create(
            order=self.order,
            payment_order=self.payment,
            beneficiary=self.customer,
            source_type="admin",
            source_reference="offline",
            idempotency_key="offline-refund",
            service_fee_refund_amount=1,
            transport_fee_refund_amount=0,
            other_fee_refund_amount=0,
            refund_amount=1,
            external_refund_amount=1,
            reason="offline",
        )
        with (
            patch("orders.services.get_huifu_payment_gateway") as http,
            self.assertRaisesMessage(ValidationError, "分账回退"),
        ):
            process_provider_order_refund(refund.refund_no)
        http.assert_not_called()

    def test_command_requires_confirmation_and_inspect_is_read_only(self):
        with patch("requests.sessions.Session.post") as http, self.assertRaises(CommandError):
            call_command("provider_distribution", "execute", self.order.order_no)
        http.assert_not_called()
        output = StringIO()
        call_command("provider_distribution", "inspect", self.order.order_no, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_started")
