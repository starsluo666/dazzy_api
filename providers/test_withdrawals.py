"""Offline earning -> reserve -> cash -> query reconciliation, no real funds."""

import json
from types import SimpleNamespace
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from orders import test_distributions as distribution
from orders.distributions import query_distribution, _save_result
from orders.distribution_transport import DistributionUncertain
from orders.huifu import HuifuPaymentConfig
from orders.models import ProviderOrderDistribution
from . import test_receiving_onboarding as onboarding
from .income_wallet import credit_distribution
from .models import ProviderProfile, ProviderIncomeWallet, ProviderIncomeEntry, ProviderWithdrawal
from .receiving_accounts import encrypt_details
from .test_huifu_user_transport import signed_http_response, http_response
from .withdrawal_gateway import HuifuWithdrawalGateway
from .withdrawals import create_withdrawal, query_withdrawal, save_result, withdrawal_data, income_wallet_data, _fingerprint


SETTINGS = {
    **distribution.SETTINGS,
    "HUIFU_PROVIDER_WITHDRAWAL_ENABLED": True,
    "HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS": 10000,
}


def balance_response(payload, amount="70.00"):
    return {
        "resp_code": "00000000",
        "req_date": payload["req_date"],
        "req_seq_id": payload["req_seq_id"],
        "acctInfo_list": json.dumps(
            [
                {
                    "huifu_id": onboarding.USER_ID,
                    "acct_id": "B00000001",
                    "acct_type": "01",
                    "acct_stat": "N",
                    "balance_amt": amount,
                    "avl_bal": amount,
                    "frz_bal": "0.00",
                }
            ]
        ),
    }


def cash_query_response(record, **changes):
    return {
        "resp_code": "00000000",
        "org_req_date": record.req_date,
        "org_req_seq_id": record.req_seq_id,
        "org_hf_seq_id": "CASH1",
        "cash_amt": f"{record.amount / 100:.2f}",
        "fee_amt": "0.10",
        "trans_status": "S",
        **changes,
    }


@override_settings(**SETTINGS)
class WithdrawalContractTests(SimpleTestCase):
    def setUp(self):
        self.gateway = HuifuWithdrawalGateway(HuifuPaymentConfig.from_settings())
        self.record = SimpleNamespace(
            amount=2000,
            req_date="20261003",
            req_seq_id="PW1",
            snapshot={"receiver_id": onboarding.USER_ID, "acct_id": "B00000001", "cash_type": "T1"},
        )

    def test_actual_sdk_uses_exact_three_routes_and_does_not_pay_from_platform(self):
        def respond(url, **kwargs):
            data = kwargs["json"]["data"]
            if url.endswith("balance/query"):
                return signed_http_response(balance_response(data))
            if url.endswith("encashment"):
                self.assertEqual(data["huifu_id"], onboarding.USER_ID)
                self.assertEqual(data["token_no"], "TESTTOKEN1")
                self.assertEqual(data["cash_amt"], "20.00")
                self.assertEqual(data["into_acct_date_type"], "T1")
                self.assertNotIn("fee_type", data)  # This is not a CITIC e-account.
                return signed_http_response(
                    {
                        "resp_code": "00000000",
                        "req_date": self.record.req_date,
                        "req_seq_id": self.record.req_seq_id,
                        "huifu_id": onboarding.USER_ID,
                        "trans_stat": "S",
                    }
                )
            self.assertEqual(data["org_req_seq_id"], "PW1")
            return signed_http_response(cash_query_response(self.record))

        with patch("requests.sessions.Session.post", side_effect=respond) as http:
            self.assertEqual(self.gateway.balance(onboarding.USER_ID)["available_amount"], 7000)
            self.assertEqual(self.gateway.submit(self.record, "TESTTOKEN1")["status"], "processing")
            result = self.gateway.query(self.record)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["fee_amount"], 10)
        self.assertEqual(
            [call.args[0] for call in http.call_args_list],
            [
                "https://api.huifu.com" + path
                for path in (
                    "/v2/trade/acctpayment/balance/query",
                    "/v2/trade/settlement/encashment",
                    "/v2/trade/settlement/query",
                )
            ],
        )

    def test_unsigned_tampered_and_bypassed_responses_never_prove_cash_success(self):
        data = cash_query_response(self.record)
        tampered = json.loads(signed_http_response(data).text)
        tampered["data"]["cash_amt"] = "30.00"
        for response in (
            http_response(data),
            http_response({"data": data}),
            http_response(tampered),
        ):
            with (
                patch("requests.sessions.Session.post", return_value=response),
                self.assertRaises(DistributionUncertain),
            ):
                self.gateway.query(self.record)
        with (
            patch("dg_sdk.V2TradeSettlementQueryRequest.post", return_value=data),
            self.assertRaises(DistributionUncertain),
        ):
            self.gateway.query(self.record)

    def test_cash_query_checks_original_identity_amount_and_known_fees(self):
        for changes in (
            {"org_req_seq_id": "OTHER"},
            {"org_req_date": "20200101"},
            {"cash_amt": "19.90"},
            {"fee_amt": None},
            {"trans_status": "X"},
        ):
            with (
                self.subTest(changes=changes),
                patch(
                    "requests.sessions.Session.post",
                    return_value=signed_http_response(cash_query_response(self.record, **changes)),
                ),
                self.assertRaises(DistributionUncertain),
            ):
                self.gateway.query(self.record)

    def test_not_found_stays_unknown_and_returned_money_needs_manual_reconciliation(self):
        for changes, expected in (
            ({"resp_code": "23000001"}, "unknown"),
            ({"trans_status": "P"}, "processing"),
            ({"trans_status": "F"}, "failed"),
            ({"re_exchange": "Y"}, "attention"),
        ):
            with patch(
                "requests.sessions.Session.post",
                return_value=signed_http_response(cash_query_response(self.record, **changes)),
            ):
                self.assertEqual(self.gateway.query(self.record)["status"], expected)

    def test_balance_requires_matching_normal_basic_account(self):
        for changes in (
            {"huifu_id": "wrong"},
            {"acct_type": "03"},
            {"acct_stat": "F"},
            {"balance_amt": "69.00"},
            {"avl_bal": "NaN"},
        ):

            def respond(url, **kwargs):
                result = balance_response(kwargs["json"]["data"])
                rows = json.loads(result["acctInfo_list"])
                rows[0].update(changes)
                result["acctInfo_list"] = json.dumps(rows)
                return signed_http_response(result)

            with (
                self.subTest(changes=changes),
                patch("requests.sessions.Session.post", side_effect=respond),
                self.assertRaises(DistributionUncertain),
            ):
                self.gateway.balance(onboarding.USER_ID)


@override_settings(**SETTINGS)
class WithdrawalStateTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        distribution.DistributionStateTests.setUp(self)
        self.provider.identity_status = "verified"
        self.provider.save()
        self.details = {
            "real_name": "测试达人",
            "id_number": onboarding.fixtures.synthetic_id(),
            "bank_card_number": "6222000000000000",
            "bank_province_code": "130000",
            "bank_city_code": "130400",
        }
        self.account.details_ciphertext = encrypt_details(self.provider.pk, self.details)
        self.account.cash_card_ciphertext = encrypt_details(
            self.provider.pk, {"token_no": "TESTTOKEN1"}
        )
        self.account.bank_card_masked = "**** **** **** 0000"
        self.account.bank_name = "测试银行"
        self.account.save()
        self.account.attempts.filter(kind="configure").update(settlement_config=onboarding.CASH)
        self.distribution, _, _ = distribution.DistributionStateTests.run_execute(self)
        with patch(
            "requests.sessions.Session.post",
            return_value=signed_http_response(
                distribution.confirm_receipt(self.distribution, query=True)
            ),
        ):
            self.distribution = query_distribution(self.order.order_no)
        self.wallet = ProviderIncomeWallet.objects.get(provider=self.provider)
        self.client = APIClient()
        self.client.force_authenticate(self.provider.user)
        self.url = "/api/v1/providers/me/income/withdrawals/"
        guard = patch(
            "requests.sessions.Session.post",
            side_effect=AssertionError("Unmocked network forbidden"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def query_account(self):
        return {
            "resp_code": "00000000",
            "huifu_id": onboarding.USER_ID,
            "indv_base_info": json.dumps(
                {
                    "name": self.details["real_name"],
                    "cert_type": "00",
                    "cert_no": self.details["id_number"],
                }
            ),
            "settle_config_list": "[]",
            "qry_cash_config_list": json.dumps(
                [
                    {
                        "cash_type": "T1",
                        "fix_amt": "0.10",
                        "switch_state": "1",
                        "out_cash_flag": "1",
                        "out_cash_huifuid": distribution.MERCHANT,
                        "out_cash_acct_type": "01",
                    }
                ]
            ),
            "qry_cash_card_info_list": json.dumps(
                [
                    {
                        "card_type": "1",
                        "card_name": self.details["real_name"],
                        "card_no": self.details["bank_card_number"],
                        "prov_id": "130000",
                        "area_id": "130400",
                        "status": "N",
                        "token_no": "TESTTOKEN1",
                    }
                ]
            ),
        }

    def submit(self, *, amount=2000, key=None, unknown=False):
        key = key or uuid.uuid4()

        def respond(url, **kwargs):
            payload = kwargs["json"]["data"]
            if url.endswith("basicdata/query"):
                return signed_http_response(self.query_account())
            if url.endswith("balance/query"):
                return signed_http_response(balance_response(payload))
            record = ProviderWithdrawal.objects.get(req_seq_id=payload["req_seq_id"])
            self.wallet.refresh_from_db()
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertEqual(self.wallet.reserved_amount, amount)
            self.assertEqual(record.status, "submitting")
            if unknown:
                raise TimeoutError("never-log-private-request")
            return signed_http_response(
                {
                    "resp_code": "00000000",
                    "req_date": record.req_date,
                    "req_seq_id": record.req_seq_id,
                    "huifu_id": onboarding.USER_ID,
                    "trans_stat": "S",
                }
            )

        with patch("requests.sessions.Session.post", side_effect=respond) as http:
            record, created = create_withdrawal(self.provider, amount=amount, request_key=key)
        return record, created, http

    def assert_balances(self, available, reserved, paid):
        self.wallet.refresh_from_db()
        self.assertEqual(
            (self.wallet.available_amount, self.wallet.reserved_amount, self.wallet.paid_amount),
            (available, reserved, paid),
        )

    def test_only_verified_manual_distribution_credits_once_without_fee_deduction(self):
        self.assert_balances(7000, 0, 0)
        credit_distribution(self.distribution)
        self.assert_balances(7000, 0, 0)
        self.assertEqual(ProviderIncomeEntry.objects.filter(kind="credit").count(), 1)
        self.assertEqual(self.distribution.payment_fee_amount, 60)

    def test_old_non_manual_split_cannot_credit(self):
        self.distribution.snapshot.pop("income_mode")
        ProviderIncomeEntry.objects.filter(
            distribution=self.distribution
        ).delete()  # isolated fixture only
        self.wallet.available_amount = 0
        self.wallet.save()
        credit_distribution(self.distribution)
        self.assert_balances(0, 0, 0)

    def test_successful_distribution_ignores_transient_and_late_pending_queries(self):
        for state in ("unknown", "processing", "succeeded"):
            _save_result(self.distribution.pk, {"status": state}, queried=True)
            self.distribution.refresh_from_db()
            self.wallet.refresh_from_db()
            self.assertEqual(self.distribution.status, "succeeded")
            self.assertFalse(self.distribution.attention_reason)
            self.assertFalse(self.wallet.hold_reason)
            self.assert_balances(7000, 0, 0)
        self.assertEqual(self.wallet.entries.filter(kind="credit").count(), 1)

    def test_real_distribution_conflict_is_not_cleared_by_later_success(self):
        for state in ("failed", "unknown", "succeeded"):
            _save_result(self.distribution.pk, {"status": state}, queried=True)
            self.wallet.refresh_from_db()
            self.assertTrue(self.wallet.hold_reason)
        self.assert_balances(7000, 0, 0)

    def test_distribution_query_timeout_after_success_does_not_hold_wallet(self):
        with patch("requests.sessions.Session.post", side_effect=TimeoutError("offline")):
            result = query_distribution(self.order.order_no)
        self.assertEqual(result.status, "succeeded")
        self.assertFalse(result.attention_reason)
        self.wallet.refresh_from_db()
        self.assertFalse(self.wallet.hold_reason)
        self.assertEqual(result.observations.last().status, "unknown")
        self.assert_balances(7000, 0, 0)

    def test_stale_verified_account_can_start_withdrawal_but_must_refresh_before_cash(self):
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()
        with patch("requests.sessions.Session.post") as http:
            self.assertTrue(income_wallet_data(self.provider)["can_withdraw"])
            http.assert_not_called()
        record, _, http = self.submit()
        self.assertEqual(record.status, "processing")
        self.assertEqual(http.call_count, 3)
        self.assertTrue(http.call_args_list[0].args[0].endswith("basicdata/query"))

    def test_stale_failed_refresh_cannot_reserve_or_send_cash(self):
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()
        with patch("providers.withdrawals.refresh_onboarding"), self.assertRaises(ValidationError):
            create_withdrawal(self.provider, amount=2000, request_key=uuid.uuid4())
        self.assertFalse(ProviderWithdrawal.objects.exists())
        self.assert_balances(7000, 0, 0)

    def test_income_still_blocks_invalid_account_configuration(self):
        for field, value in (
            ("channel_checked_at", None),
            ("channel_checked_at", self.now + timedelta(days=1)),
            ("automatic_settlement_disabled", False),
            ("channel_status", "attention"),
            ("cash_status", "F"),
        ):
            original = getattr(self.account, field)
            setattr(self.account, field, value)
            self.account.save()
            self.assertFalse(income_wallet_data(self.provider)["can_withdraw"])
            setattr(self.account, field, original)
            self.account.save()

    def test_reencrypting_same_card_keeps_fingerprint_but_changed_token_does_not(self):
        from .cash_accounts import verify_cash_configuration

        first = _fingerprint(self.account)
        self.assertTrue(verify_cash_configuration(self.account, self.query_account(), self.details, onboarding.CASH))
        self.assertEqual(_fingerprint(self.account), first)
        self.account.cash_card_ciphertext = encrypt_details(self.provider.pk, {"token_no": "DIFFERENT1"})
        self.assertNotEqual(_fingerprint(self.account), first)

    def test_concurrent_same_card_refresh_during_balance_query_does_not_block_cash(self):
        original_balance = HuifuWithdrawalGateway.balance

        def balance(gateway, receiver_id):
            result = original_balance(gateway, receiver_id)
            self.account.refresh_from_db()
            self.account.cash_card_ciphertext = encrypt_details(self.provider.pk, {"token_no": "TESTTOKEN1"})
            self.account.save(update_fields=["cash_card_ciphertext"])
            return result

        with patch.object(HuifuWithdrawalGateway, "balance", balance):
            record, _, _ = self.submit()
        self.assertEqual(record.status, "processing")
        self.assert_balances(5000, 2000, 0)

    def test_concurrent_changed_card_during_balance_query_blocks_cash(self):
        original_balance = HuifuWithdrawalGateway.balance

        def balance(gateway, receiver_id):
            result = original_balance(gateway, receiver_id)
            self.account.refresh_from_db()
            self.account.cash_card_ciphertext = encrypt_details(self.provider.pk, {"token_no": "DIFFERENT1"})
            self.account.save(update_fields=["cash_card_ciphertext"])
            return result

        with patch.object(HuifuWithdrawalGateway, "balance", balance), self.assertRaises(ValidationError):
            self.submit()
        self.assertFalse(ProviderWithdrawal.objects.exists())
        self.assert_balances(7000, 0, 0)

    def test_successful_distribution_has_ready_local_plan_and_separate_channel_fees(self):
        from orders.settlement_plans import sync_provider_settlement_plan
        from backoffice.serializers import ProviderOrderSettlementSerializer

        plan = sync_provider_settlement_plan(order_no=self.order.order_no)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.blockers, [])
        self.settlement.refresh_from_db()
        result = ProviderOrderSettlementSerializer(self.settlement).data
        self.assertEqual(result["distribution"]["status"], "succeeded")
        self.assertEqual(result["distribution"]["split_fee_amount"], 20)
        self.assertFalse(result["distribution_plan"]["execution_enabled"])

    def unpaid_order(self):
        from orders.models import ProviderOrder

        fields = (
            "customer_id", "provider_id", "service_id", "provider_name_snapshot",
            "service_name_snapshot", "billing_type_snapshot", "unit_price_amount",
            "service_fee_amount", "transport_fee_amount", "other_fee_amount", "payable_amount",
            "pricing_snapshot", "duration_minutes", "meeting_address", "contact_name", "contact_phone",
        )
        return ProviderOrder.objects.create(
            **{key: getattr(self.order, key) for key in fields},
            order_no="PAY" + uuid.uuid4().hex[:24],
            starts_at=self.now + timedelta(hours=3), ends_at=self.now + timedelta(hours=5),
            payment_expires_at=self.now + timedelta(minutes=15), status="pending_payment",
        )

    def payment_session(self, order):
        from orders.services import create_provider_order_huifu_payment_session

        # OAuth identity is covered by its own suite; use this test payer here.
        with patch("orders.wechat_oauth.validate_wechat_payment_payer"):
            return create_provider_order_huifu_payment_session(
                order_id=order.pk, customer_id=order.customer_id,
                payment_scene="official_account", sub_openid="offline",
            )

    def test_expired_payment_account_refreshes_outside_transaction_then_creates_once(self):
        from orders.huifu import HuifuAggregatePaymentGateway, HuifuPaymentSessionResult
        from orders.models import ProviderOrderPaymentOrder
        from wallets.models import WalletPaymentAllocation

        order = self.unpaid_order()
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()

        def account_query(url, **kwargs):
            self.assertTrue(url.endswith("basicdata/query"))
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertFalse(ProviderOrderPaymentOrder.objects.filter(order=order).exists())
            self.assertFalse(WalletPaymentAllocation.objects.filter(business_order_no=order.order_no).exists())
            return signed_http_response(self.query_account())

        def create(**kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertEqual(kwargs["delay_acct_flag"], "Y")
            payment = ProviderOrderPaymentOrder.objects.get(order=order)
            self.assertEqual(payment.preorder_attempts, 1)
            return HuifuPaymentSessionResult(
                req_seq_id=kwargs["req_seq_id"], req_date=kwargs["req_date"],
                huifu_id=distribution.MERCHANT, trade_type="T_JSAPI", trans_stat="P",
                hf_seq_id="OFFLINE-PAY", party_order_id="", out_trans_id="",
                pay_info={"package": "prepay_id=offline"}, response_code="00000000", response_digest="offline",
            )

        with (
            patch("requests.sessions.Session.post", side_effect=account_query) as query,
            patch.object(HuifuAggregatePaymentGateway, "create_payment", side_effect=create) as payment_call,
        ):
            _, created = self.payment_session(order)
            self.assertTrue(created)
            _, created = self.payment_session(order)
            self.assertFalse(created)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(payment_call.call_count, 1)

    def test_payment_refresh_failure_never_creates_payment_or_falls_back_to_normal(self):
        from orders.huifu import HuifuAggregatePaymentGateway
        from orders.models import ProviderOrderPaymentOrder

        order = self.unpaid_order()
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()
        with (
            patch("requests.sessions.Session.post", side_effect=TimeoutError("offline")) as query,
            patch.object(HuifuAggregatePaymentGateway, "create_payment") as create,
            self.assertRaises(ValidationError),
        ):
            self.payment_session(order)
        self.assertEqual(query.call_count, 1)
        create.assert_not_called()
        self.assertFalse(ProviderOrderPaymentOrder.objects.filter(order=order).exists())

    def test_payment_registered_during_refresh_is_replayed_not_resent(self):
        from orders.huifu import HuifuAggregatePaymentGateway
        from orders.services import create_provider_order_payment_order

        order = self.unpaid_order()
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()

        def query(url, **kwargs):
            payment, _ = create_provider_order_payment_order(order)
            payment.req_seq_id = "CONCURRENT-PAY"
            payment.req_date = timezone.localdate().strftime("%Y%m%d")
            payment.gateway_merchant_id = distribution.MERCHANT
            payment.preorder_status = "ready"
            payment.payment_scene = "official_account"
            payment.trade_type = "T_JSAPI"
            payment.payment_invoke_payload = {"package": "prepay_id=existing"}
            payment.save()
            return signed_http_response(self.query_account())

        with (
            patch("requests.sessions.Session.post", side_effect=query),
            patch.object(HuifuAggregatePaymentGateway, "create_payment") as create,
        ):
            result, created = self.payment_session(order)
        self.assertFalse(created)
        self.assertEqual(result.pay_info, {"package": "prepay_id=existing"})
        create.assert_not_called()

    def test_payment_rechecks_expiry_after_account_refresh(self):
        from orders.huifu import HuifuAggregatePaymentGateway

        order = self.unpaid_order()
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()

        def query(url, **kwargs):
            order.payment_expires_at = self.now - timedelta(minutes=1)
            order.save(update_fields=["payment_expires_at"])
            return signed_http_response(self.query_account())

        with (
            patch("requests.sessions.Session.post", side_effect=query),
            patch.object(HuifuAggregatePaymentGateway, "create_payment") as create,
            self.assertRaises(ValidationError),
        ):
            self.payment_session(order)
        create.assert_not_called()

    def test_payment_rechecks_unsafe_account_after_refresh(self):
        from orders.huifu import HuifuAggregatePaymentGateway

        order = self.unpaid_order()
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()
        response = self.query_account()
        response["settle_config_list"] = '[{"settle_status":"1"}]'
        with (
            patch("requests.sessions.Session.post", return_value=signed_http_response(response)),
            patch.object(HuifuAggregatePaymentGateway, "create_payment") as create,
            self.assertRaises(ValidationError),
        ):
            self.payment_session(order)
        create.assert_not_called()

    def test_invalid_payment_owner_never_triggers_account_refresh(self):
        from orders.services import create_provider_order_huifu_payment_session
        from orders.models import ProviderOrder

        order = self.unpaid_order()
        self.account.channel_checked_at = self.now - timedelta(minutes=31)
        self.account.save()
        with patch("requests.sessions.Session.post") as query, self.assertRaises(ProviderOrder.DoesNotExist):
            create_provider_order_huifu_payment_session(
                order_id=order.pk, customer_id=self.provider.user_id,
                payment_scene="official_account", sub_openid="offline",
            )
        query.assert_not_called()

    def test_reserve_is_committed_before_io_and_repeat_key_never_dispatches(self):
        record, created, http = self.submit()
        self.assertTrue(created)
        self.assertEqual(record.status, "processing")
        self.assertEqual(http.call_count, 3)
        self.assert_balances(5000, 2000, 0)
        with patch("requests.sessions.Session.post") as network:
            replay, created = create_withdrawal(
                self.provider, amount=2000, request_key=record.request_key
            )
            with self.assertRaises(ValidationError):
                create_withdrawal(self.provider, amount=2100, request_key=record.request_key)
            network.assert_not_called()
        self.assertFalse(created)
        self.assertEqual(replay.pk, record.pk)
        self.assertNotIn("TESTTOKEN1", json.dumps(record.snapshot))
        self.assertNotIn(onboarding.USER_ID, str(withdrawal_data(record)))

    def test_second_different_request_and_overspend_are_blocked(self):
        self.submit()
        for amount in (1, 7000, 0, -1):
            with (
                self.subTest(amount=amount),
                patch("requests.sessions.Session.post") as http,
                self.assertRaises(ValidationError),
            ):
                create_withdrawal(self.provider, amount=amount, request_key=uuid.uuid4())
            http.assert_not_called()
        self.assertEqual(ProviderWithdrawal.objects.count(), 1)

    def test_timeout_keeps_reservation_and_process_crash_cannot_resend(self):
        record, _, _ = self.submit(unknown=True)
        self.assertEqual(record.status, "unknown")
        self.assert_balances(5000, 2000, 0)
        record.status = "submitting"
        record.save()
        with patch("requests.sessions.Session.post") as http:
            create_withdrawal(self.provider, amount=2000, request_key=record.request_key)
            http.assert_not_called()

    def test_success_query_consumes_once_and_late_ack_does_not_regress(self):
        record, _, _ = self.submit()
        with patch(
            "requests.sessions.Session.post",
            return_value=signed_http_response(cash_query_response(record)),
        ):
            query_withdrawal(record)
            query_withdrawal(record)
        save_result(record.pk, {"status": "processing"}, queried=False)
        self.assert_balances(5000, 0, 2000)
        record.refresh_from_db()
        self.assertEqual(record.status, "succeeded")
        self.assertEqual(record.fee_amount, 10)  # Platform pays; provider keeps the full amount.
        self.assertEqual(ProviderIncomeEntry.objects.filter(kind="paid").count(), 1)

    def test_failure_releases_only_after_original_query_and_sufficient_channel_balance(self):
        record, _, _ = self.submit()

        def respond(url, **kwargs):
            return signed_http_response(
                balance_response(kwargs["json"]["data"])
                if url.endswith("balance/query")
                else cash_query_response(record, trans_status="F")
            )

        with patch("requests.sessions.Session.post", side_effect=respond):
            query_withdrawal(record)
            query_withdrawal(record)
        self.assert_balances(7000, 0, 0)
        self.assertEqual(ProviderIncomeEntry.objects.filter(kind="release").count(), 1)

    def test_failure_without_returned_funds_stays_frozen(self):
        record, _, _ = self.submit()

        def respond(url, **kwargs):
            return signed_http_response(
                balance_response(kwargs["json"]["data"], "50.00")
                if url.endswith("balance/query")
                else cash_query_response(record, trans_status="F")
            )

        with patch("requests.sessions.Session.post", side_effect=respond):
            self.assertEqual(query_withdrawal(record).status, "unknown")
        self.assert_balances(5000, 2000, 0)

    def test_return_after_success_holds_account_instead_of_inventing_a_credit(self):
        record, _, _ = self.submit()
        with patch(
            "requests.sessions.Session.post",
            return_value=signed_http_response(cash_query_response(record)),
        ):
            query_withdrawal(record)
        with patch(
            "requests.sessions.Session.post",
            return_value=signed_http_response(cash_query_response(record, re_exchange="Y")),
        ):
            self.assertEqual(query_withdrawal(record).status, "attention")
        self.assert_balances(5000, 0, 2000)
        self.assertTrue(self.wallet.hold_reason)

    def test_distribution_conflict_holds_existing_wallet(self):
        _save_result(self.distribution.pk, {"status": "failed"}, queried=True)
        self.wallet.refresh_from_db()
        self.assertTrue(self.wallet.hold_reason)
        with self.assertRaises(ValidationError):
            create_withdrawal(self.provider, amount=1, request_key=uuid.uuid4())

    def test_switch_allowlist_limit_and_channel_change_fail_closed(self):
        for changes in (
            {"HUIFU_PROVIDER_WITHDRAWAL_ENABLED": False},
            {"HUIFU_PROVIDER_DISTRIBUTION_IDS": ()},
            {"HUIFU_PROVIDER_WITHDRAWAL_MAX_CENTS": 1},
            {"HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED": False},
        ):
            with (
                self.settings(**changes),
                patch("requests.sessions.Session.post") as http,
                self.assertRaises(ValidationError),
            ):
                create_withdrawal(self.provider, amount=2000, request_key=uuid.uuid4())
            http.assert_not_called()
        record, _, _ = self.submit()
        with (
            self.settings(HUIFU_PRODUCT_ID="OTHER"),
            patch("requests.sessions.Session.post") as http,
            self.assertRaises(ValidationError),
        ):
            query_withdrawal(record)
        http.assert_not_called()

    def test_query_allowed_with_withdrawal_switch_off(self):
        record, _, _ = self.submit()
        with (
            self.settings(HUIFU_PROVIDER_WITHDRAWAL_ENABLED=False),
            patch(
                "requests.sessions.Session.post",
                return_value=signed_http_response(cash_query_response(record)),
            ),
        ):
            self.assertEqual(query_withdrawal(record).status, "succeeded")

    def test_channel_enables_auto_settlement_again_no_cash_dispatched(self):
        data = self.query_account()
        data["settle_config_list"] = '[{"settle_status":"1"}]'
        with (
            patch(
                "requests.sessions.Session.post", return_value=signed_http_response(data)
            ) as http,
            self.assertRaises(ValidationError),
        ):
            create_withdrawal(self.provider, amount=2000, request_key=uuid.uuid4())
        self.assertEqual(http.call_count, 1)
        self.assertEqual(ProviderWithdrawal.objects.count(), 0)
        self.assert_balances(7000, 0, 0)

    def test_stale_refresh_or_unconfirmed_channel_balance_cannot_reserve(self):
        with patch("providers.withdrawals.refresh_onboarding"), self.assertRaises(ValidationError):
            create_withdrawal(self.provider, amount=2000, request_key=uuid.uuid4())

        def respond(url, **kwargs):
            if url.endswith("basicdata/query"):
                return signed_http_response(self.query_account())
            self.assertTrue(url.endswith("balance/query"))
            return signed_http_response(balance_response(kwargs["json"]["data"], amount="69.99"))

        with (
            patch("requests.sessions.Session.post", side_effect=respond),
            self.assertRaises(ValidationError),
        ):
            create_withdrawal(self.provider, amount=2000, request_key=uuid.uuid4())
        self.assertFalse(ProviderWithdrawal.objects.exists())
        self.assert_balances(7000, 0, 0)

    def test_cross_provider_and_anonymous_cannot_query_withdrawal(self):
        record, _, _ = self.submit()
        user = User.objects.create_user(phone="19900000999", password="offline-only")
        ProviderProfile.objects.create(user=user, status="approved")
        url = self.url + record.req_seq_id + "/refresh/"
        self.client.force_authenticate(user)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(url).status_code, 401)
        self.assertEqual(
            self.client.post(
                self.url, {"amount": 1, "request_key": str(uuid.uuid4())}, format="json"
            ).status_code,
            401,
        )

    def test_income_is_read_only_and_exposes_only_safe_balance_fields(self):
        self.submit()
        with patch("requests.sessions.Session.post") as http:
            result = self.client.get("/api/v1/providers/me/income/")
            http.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["data"]["wallet"]["available_amount"], 5000)
        self.assertNotIn(onboarding.USER_ID, str(result.data))
        self.assertNotIn("TESTTOKEN1", str(result.data))
        self.assertNotIn(self.details["bank_card_number"], str(result.data))

    def test_income_or_reserved_funds_block_account_closure(self):
        from accounts.account_closure import account_closure_blockers

        self.assertIn(
            "provider_income_wallet",
            [row["code"] for row in account_closure_blockers(self.provider.user)],
        )

    def test_uncertain_query_never_releases_balance(self):
        record, _, _ = self.submit()
        with patch(
            "requests.sessions.Session.post", return_value=http_response({"resp_code": "00000000"})
        ):
            self.assertEqual(query_withdrawal(record).status, "unknown")
        self.assert_balances(5000, 2000, 0)

    def test_jobs_are_default_closed_and_do_not_even_query(self):
        from .tasks import process_income_transfers

        with (
            self.settings(HUIFU_PROVIDER_INCOME_JOBS_ENABLED=False),
            patch("requests.sessions.Session.post") as http,
        ):
            self.assertEqual(process_income_transfers(), {"disabled": True})
            http.assert_not_called()

    def test_jobs_only_query_existing_withdrawals_never_recreate_them(self):
        from .tasks import process_income_transfers

        record, _, _ = self.submit(unknown=True)
        with (
            self.settings(
                HUIFU_PROVIDER_INCOME_JOBS_ENABLED=True, HUIFU_PROVIDER_DISTRIBUTION_ENABLED=False
            ),
            patch(
                "requests.sessions.Session.post",
                return_value=signed_http_response(cash_query_response(record)),
            ) as http,
        ):
            result = process_income_transfers()
        self.assertEqual(result["withdrawal_queried"], 1)
        self.assertTrue(
            all(call.args[0].endswith("/settlement/query") for call in http.call_args_list)
        )
        self.assert_balances(5000, 0, 2000)

    def test_post_endpoint_replays_same_request_without_network(self):
        record, _, _ = self.submit()
        with patch("requests.sessions.Session.post") as http:
            result = self.client.post(
                self.url, {"amount": 2000, "request_key": str(record.request_key)}, format="json"
            )
            http.assert_not_called()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["data"]["withdrawal_no"], record.req_seq_id)


@override_settings(**{**SETTINGS, "HUIFU_PROVIDER_INCOME_JOBS_ENABLED": True})
class IncomeJobTests(TransactionTestCase):
    def setUp(self):
        distribution.DistributionStateTests.setUp(self)
        self.details = {
            "real_name": "测试达人",
            "id_number": onboarding.fixtures.synthetic_id(),
            "bank_card_number": "6222000000000000",
            "bank_province_code": "130000",
            "bank_city_code": "130400",
        }
        self.account.details_ciphertext = encrypt_details(self.provider.pk, self.details)
        self.account.save()
        self.account.attempts.filter(kind="configure").update(settlement_config=onboarding.CASH)

    def respond(self, url, **kwargs):
        if url.endswith("basicdata/query"):
            return signed_http_response(WithdrawalStateTests.query_account(self))
        if url.endswith("scanpay/query"):
            snap = {
                **distribution.snapshot(),
                "payment": {
                    "req_date": self.payment.req_date,
                    "req_seq_id": self.payment.req_seq_id,
                    "gateway_trade_no": self.payment.gateway_trade_no,
                },
            }
            return signed_http_response(distribution.payment_receipt(snap))
        self.assertIn(url.rsplit("/", 1)[-1], ("confirm", "confirmquery"))
        record = ProviderOrderDistribution.objects.get(settlement=self.settlement)
        return signed_http_response(
            distribution.confirm_receipt(record, query=url.endswith("confirmquery"))
        )

    def test_eligible_order_credits_income_once_and_never_automatically_withdraws(self):
        from .tasks import process_income_transfers

        with patch("requests.sessions.Session.post", side_effect=self.respond) as http:
            result = process_income_transfers()
            repeated = process_income_transfers()
        self.assertEqual(result["distribution_submitted"], 1)
        self.assertEqual(result["distribution_queried"], 1)
        self.assertEqual(repeated["distribution_submitted"], 0)
        self.assertEqual(http.call_count, 4)
        wallet = ProviderIncomeWallet.objects.get(provider=self.provider)
        self.assertEqual(wallet.available_amount, 7000)
        self.assertEqual(wallet.entries.filter(kind="credit").count(), 1)
        self.assertFalse(ProviderWithdrawal.objects.exists())

    def test_freeze_unexpired_and_nonpilot_orders_do_not_send_distribution(self):
        from .tasks import process_income_transfers

        self.settlement.freeze_until = timezone.now() + timezone.timedelta(days=1)
        self.settlement.save()
        with patch("requests.sessions.Session.post") as http:
            self.assertEqual(process_income_transfers()["distribution_submitted"], 0)
            http.assert_not_called()
        self.settlement.freeze_until = timezone.now() - timezone.timedelta(days=1)
        self.settlement.save()
        self.payment.delay_acct_flag = "N"
        self.payment.save()
        with patch("requests.sessions.Session.post") as http:
            self.assertEqual(process_income_transfers()["distribution_submitted"], 0)
            http.assert_not_called()
