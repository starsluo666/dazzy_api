"""Offline account-payment contract tests: real SDK/RSA, mocked HTTP, no DB."""

import base64
from dataclasses import replace
import json
from unittest.mock import patch

from Crypto.Hash import SHA256
from Crypto.Signature import pkcs1_15
from django.test import SimpleTestCase, override_settings

from providers.test_huifu_user_transport import signed_http_response, http_response
from providers.test_receiving_onboarding import KEY, USER_ID
from .balance_payment_gateway import BalancePaymentIntent, HuifuBalancePaymentGateway, payment_payload
from .distribution_transport import DistributionEvidenceConflict, DistributionPreflightError, DistributionUncertain
from .huifu import HuifuPaymentConfig
from .test_distributions import MERCHANT, SETTINGS


def intent(**changes):
    # Synthetic business data for offline tests, never production defaults.
    return replace(BalancePaymentIntent(
        req_date="20261019", req_seq_id="BP-OFFLINE-1", merchant_id=MERCHANT,
        receiver_id=USER_ID, amount=7000, transfer_type="05", risk_ip="192.0.2.10",
        fee_mode="request_payer",
    ), **changes)


def receipt(*, query=True, **changes):
    data = {
        "resp_code": "00000000", "resp_desc": "交易成功",
        "huifu_id": MERCHANT, "out_huifu_id": MERCHANT,
        "ord_amt": "70.00", "trans_stat": "S", "delay_acct_flag": "N",
        "trans_fee_take_flag": "OUT", "trans_finish_time": "20261019120000",
        "acct_split_bunch": json.dumps({
            "acct_infos": [{"huifu_id": USER_ID, "div_amt": "70.00"}],
            "fee_amt": "0.12", "fee_huifu_id": MERCHANT,
        }),
    }
    if query:
        data.update(org_req_date="20261019", org_req_seq_id="BP-OFFLINE-1", org_hf_seq_id="BP-HF-OFFLINE-1")
    else:
        data.update(req_date="20261019", req_seq_id="BP-OFFLINE-1", hf_seq_id="BP-HF-OFFLINE-1")
    return {**data, **changes}


def with_split(data, *, row=None, **changes):
    split = json.loads(data["acct_split_bunch"])
    if row is not None:
        split["acct_infos"][0].update(row)
    split.update(changes)
    return {**data, "acct_split_bunch": json.dumps(split)}


@override_settings(**SETTINGS)
class BalancePaymentGatewayTests(SimpleTestCase):
    def setUp(self):
        self.config = HuifuPaymentConfig.from_settings()
        self.gateway = HuifuBalancePaymentGateway(self.config)

    def query(self, data, **kwargs):
        with patch("requests.sessions.Session.post", return_value=signed_http_response(data)) as http:
            result = self.gateway.query(intent(), **kwargs)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(http.call_args.args[0], "https://api.huifu.com/v2/trade/acctpayment/pay/query")
        return result

    def test_submission_disabled_by_default_even_when_distribution_flags_are_on(self):
        with patch("requests.sessions.Session.post") as http, self.assertRaises(DistributionPreflightError) as error:
            self.gateway.pay(intent())
        self.assertEqual(error.exception.code, "account_payment_disabled")
        http.assert_not_called()
        self.assertEqual(self.query(receipt())["status"], "succeeded")

    def test_actual_sdk_request_signing_routes_and_query_use_original_account_payment(self):
        from dg_sdk import DGClient
        from dg_sdk.core.api_request import ApiRequest

        parser, builder = ApiRequest.__dict__["_build_return_data"], ApiRequest.__dict__["_build_request_info"]
        previous = {key: getattr(DGClient, key, None) for key in ("env", "mer_config", "connect_timeout")}
        gateway = HuifuBalancePaymentGateway(self.config, allow_submit=True)
        replies = iter((receipt(query=False), receipt()))

        def post(session, url, **kwargs):
            self.assertTrue(session.verify)
            self.assertNotIn("verify", kwargs)
            self.assertEqual(kwargs["timeout"], self.config.connect_timeout_seconds)
            body = kwargs["json"]
            canonical = json.dumps(dict(sorted(body["data"].items())), ensure_ascii=False, separators=(",", ":"))
            pkcs1_15.new(KEY.public_key()).verify(SHA256.new(canonical.encode()), base64.b64decode(body["sign"]))
            self.assertEqual(body["sys_id"], self.config.sys_id)
            self.assertEqual(body["product_id"], self.config.product_id)
            self.assertEqual(kwargs["headers"]["jpt-x-skill-source"], self.config.skill_source)
            if url.endswith("/query"):
                self.assertEqual(body["data"], {
                    "huifu_id": MERCHANT, "org_req_date": "20261019", "org_req_seq_id": "BP-OFFLINE-1",
                })
            else:
                self.assertEqual(url, "https://api.huifu.com/v2/trade/acctpayment/pay")
                self.assertEqual(body["data"], payment_payload(intent()))
                self.assertNotIn("huifu_id", body["data"])
                self.assertNotIn("notify_url", body["data"])
                self.assertNotIn("fund_type", body["data"])
            return signed_http_response(next(replies))

        with patch("requests.sessions.Session.post", autospec=True, side_effect=post) as http:
            accepted = gateway.pay(intent())
            result = gateway.query(intent())
        self.assertEqual(http.call_count, 2)
        self.assertEqual(accepted["status"], "processing")
        self.assertIsNone(accepted["channel_fee_amount"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["channel_fee_amount"], 12)
        self.assertEqual(result["gateway_trade_no"], "BP-HF-OFFLINE-1")
        self.assertIs(ApiRequest.__dict__["_build_return_data"], parser)
        self.assertIs(ApiRequest.__dict__["_build_request_info"], builder)
        self.assertEqual({key: getattr(DGClient, key, None) for key in previous}, previous)

    def test_fee_mode_and_optional_accounts_and_snake_case_risk_fields(self):
        payload = payment_payload(intent(fee_mode="merchant_payer", out_acct_id="SOURCE1", receiver_acct_id="TARGET1"))
        self.assertNotIn("trans_fee_take_flag", payload)
        self.assertEqual(payload["out_acct_id"], "SOURCE1")
        self.assertEqual(json.loads(payload["acct_split_bunch"]), {
            "acct_infos": [{"huifu_id": USER_ID, "div_amt": "70.00", "acct_id": "TARGET1"}],
        })
        self.assertEqual(json.loads(payload["risk_check_data"]), {"transfer_type": "05", "ip_addr": "192.0.2.10"})
        self.assertEqual(payload["delay_acct_flag"], "N")

    def test_invalid_intents_never_contact_channel(self):
        changes = (
            {"req_date": "20260230"}, {"req_date": "2026111"}, {"req_seq_id": ""},
            {"req_seq_id": "a" * 129}, {"merchant_id": "OTHER"}, {"receiver_id": MERCHANT},
            {"receiver_id": ""}, {"amount": 0}, {"amount": -1}, {"amount": True},
            {"amount": 1.0}, {"amount": "100"}, {"amount": 10**13},
            {"transfer_type": ""}, {"transfer_type": "salary"}, {"fee_mode": "recipient"},
            {"fee_mode": ""}, {"risk_ip": ""}, {"risk_ip": "127.0.0.1"},
            {"risk_ip": "0.0.0.0"}, {"risk_ip": "not-an-ip"}, {"risk_ip": "224.0.0.1"},
            {"out_acct_id": "a" * 33}, {"receiver_acct_id": None},
        )
        gateway = HuifuBalancePaymentGateway(self.config, allow_submit=True)
        for change in changes:
            with self.subTest(change=change), patch("requests.sessions.Session.post") as http:
                with self.assertRaises(DistributionPreflightError):
                    gateway.pay(intent(**change))
                http.assert_not_called()

    def test_sync_success_pending_and_failure_cannot_credit_provider(self):
        gateway = HuifuBalancePaymentGateway(self.config, allow_submit=True)
        for code, state, expected in (
            ("00000000", "S", "processing"), ("00000100", "P", "processing"),
            ("00000000", "F", "processing"), ("90000000", "F", "unknown"),
            ("98888888", "S", "unknown"), ("00000000", "C", "unknown"),
        ):
            with self.subTest(code=code, state=state), patch(
                "requests.sessions.Session.post", return_value=signed_http_response(receipt(query=False, resp_code=code, trans_stat=state)),
            ) as http:
                result = gateway.pay(intent())
                self.assertEqual(result["status"], expected)
                self.assertIsNone(result["channel_fee_amount"])
                self.assertEqual(http.call_count, 1)

    def test_query_states_and_not_found_are_not_permission_to_retry(self):
        for code, state, expected in (
            ("00000000", "S", "succeeded"), ("00000000", "P", "processing"),
            ("00000000", "F", "failed"), ("00000000", "C", "unknown"),
            ("23000001", "F", "unknown"), ("98888888", "S", "unknown"),
            ("00000100", "S", "unknown"), ("00000000", "", "unknown"),
        ):
            with self.subTest(code=code, state=state):
                result = self.query(receipt(resp_code=code, trans_stat=state))
                self.assertEqual(result["status"], expected)
                if expected != "succeeded":
                    self.assertIsNone(result["channel_fee_amount"])

    def test_signed_identity_amount_receiver_and_fee_conflicts(self):
        variants = [
            ({**receipt(), key: value}, expected) for key, value, expected in (
                ("org_req_seq_id", "CUSTOMER-PAY", "account_query_identity_conflict"),
                ("org_req_date", "20261018", "account_query_identity_conflict"),
                ("huifu_id", "OTHER", "account_query_identity_conflict"),
                ("out_huifu_id", "OTHER", "account_query_identity_conflict"),
                ("ord_amt", "100.00", "account_query_amount_conflict"),
                ("delay_acct_flag", "Y", "account_query_delay_conflict"),
                ("unconfirm_amt", "1.00", "account_query_delay_conflict"),
                ("trans_fee_take_flag", "IN", "account_query_fee_bearer_conflict"),
            )
        ]
        variants += [
            (with_split(receipt(), row={key: value}), expected) for key, value, expected in (
                ("huifu_id", "OTHER", "account_query_receiver_conflict"),
                ("div_amt", "69.99", "account_query_amount_conflict"),
                ("fee_amt", "0.01", "account_query_fee_bearer_conflict"),
                ("trans_stat", "P", "account_query_receiver_state_conflict"),
                ("unconfirm_amt", "70.00", "account_query_delay_conflict"),
            )
        ]
        variants += [
            (with_split(receipt(), acct_infos=[]), "account_query_receiver_conflict"),
            (with_split(receipt(), fee_huifu_id=USER_ID), "account_query_fee_bearer_conflict"),
        ]
        for data, expected in variants:
            with self.subTest(expected=expected, data=data), self.assertRaises(DistributionEvidenceConflict) as error:
                self.query(data)
            self.assertEqual(error.exception.code, expected)
            self.assertEqual(error.exception.response_code, "00000000")
            self.assertEqual(len(error.exception.response_digest), 64)
        with self.assertRaises(DistributionEvidenceConflict) as error:
            self.query(receipt(), known_trade_no="ANOTHER-TRADE")
        self.assertEqual(error.exception.code, "account_query_trade_conflict")

    def test_missing_or_malformed_evidence_is_never_success(self):
        variants = [
            {key: value for key, value in receipt().items() if key != missing}
            for missing in ("out_huifu_id", "org_req_date", "org_req_seq_id", "huifu_id", "ord_amt", "acct_split_bunch", "org_hf_seq_id")
        ]
        variants += [
            receipt(ord_amt=70), receipt(ord_amt="70"), receipt(ord_amt="7e1"),
            receipt(acct_split_bunch={}), receipt(acct_split_bunch="[]"),
            receipt(acct_split_bunch='{"acct_infos": [], "acct_infos": []}'),
            receipt(acct_split_bunch='{"acct_infos": NaN}'),
            with_split(receipt(), fee_amt=None), with_split(receipt(), fee_amt="-0.12"),
            with_split(receipt(), fee_huifu_id=""),
            receipt(trans_fee_take_flag=""),
        ]
        for data in variants:
            with self.subTest(data=data), self.assertRaises(DistributionUncertain):
                self.query(data)

    def test_merchant_fee_mode_does_not_require_request_flag_but_needs_fee_evidence(self):
        data = receipt()
        data.pop("trans_fee_take_flag")
        with patch("requests.sessions.Session.post", return_value=signed_http_response(data)):
            result = self.gateway.query(intent(fee_mode="merchant_payer"))
        self.assertEqual(result["channel_fee_amount"], 12)
        self.assertEqual(self.query(with_split(receipt(), fee_amt="0.00"))["channel_fee_amount"], 0)

    def test_unknown_fee_does_not_hide_known_recipient_conflict(self):
        data = with_split(receipt(), fee_amt=None, row={"div_amt": "69.99"})
        with self.assertRaises(DistributionEvidenceConflict) as error:
            self.query(data)
        self.assertEqual(error.exception.code, "account_query_amount_conflict")

    def test_optional_receiver_account_conflict_is_checked_when_returned(self):
        data = with_split(receipt(), row={"acct_id": "ANOTHER"})
        with patch("requests.sessions.Session.post", return_value=signed_http_response(data)):
            with self.assertRaises(DistributionEvidenceConflict):
                self.gateway.query(intent(receiver_acct_id="EXPECTED"))

    def test_bad_signature_missing_signature_and_sdk_bypass_rejected(self):
        data = receipt()
        tampered = json.loads(signed_http_response(data).text)
        tampered["data"]["ord_amt"] = "700.00"
        for response in (http_response(data), http_response({"data": data}), http_response(tampered)):
            with patch("requests.sessions.Session.post", return_value=response), self.assertRaises(DistributionUncertain):
                self.gateway.query(intent())
        with patch("dg_sdk.V2TradeAcctpaymentPayQueryRequest.post", return_value=data), self.assertRaises(DistributionUncertain):
            self.gateway.query(intent())

    def test_timeout_never_resubmits_or_leaks_exception_and_restores_sdk(self):
        from dg_sdk.core.api_request import ApiRequest
        parser = ApiRequest.__dict__["_build_return_data"]
        gateway = HuifuBalancePaymentGateway(self.config, allow_submit=True)
        for action in (gateway.pay, gateway.query):
            with patch("requests.sessions.Session.post", side_effect=TimeoutError("PRIVATE-DETAIL")) as http:
                with self.assertRaises(DistributionUncertain) as error:
                    action(intent())
                self.assertNotIn("PRIVATE-DETAIL", str(error.exception))
                self.assertEqual(http.call_count, 1)
            self.assertIs(ApiRequest.__dict__["_build_return_data"], parser)

    def test_source_query_requires_original_identity_amount_and_non_split_funds(self):
        source = {"req_date": "20261001", "req_seq_id": "RECHARGE-OFFLINE", "gateway_trade_no": "SOURCE-HF", "amount": 10000}
        valid = {"resp_code": "00000000", "huifu_id": MERCHANT, "req_date": source["req_date"],
                 "req_seq_id": source["req_seq_id"], "hf_seq_id": source["gateway_trade_no"],
                 "acct_id": "BTEST001", "acct_stat": "S",
                 "trans_stat": "S", "delay_acct_flag": "N", "div_flag": "N", "trans_amt": "100.00"}
        with patch("requests.sessions.Session.post", return_value=signed_http_response(valid)) as http:
            self.assertEqual(len(self.gateway.verify_source(source, acct_id="BTEST001")), 64)
        self.assertEqual(http.call_args.args[0], "https://api.huifu.com/v4/trade/payment/scanpay/query")
        for key, value in (("huifu_id", "OTHER"), ("req_seq_id", "OTHER"), ("hf_seq_id", "OTHER"),
                           ("req_date", "20261002"), ("trans_stat", "P"), ("delay_acct_flag", "Y"),
                           ("div_flag", "Y"), ("trans_amt", "99.99"), ("acct_id", "OTHER"),
                           ("acct_stat", "P"), ("fund_freeze_stat", "FREEZE")):
            with self.subTest(key=key), patch("requests.sessions.Session.post", return_value=signed_http_response({**valid, key: value})):
                with self.assertRaises(DistributionUncertain):
                    self.gateway.verify_source(source, acct_id="BTEST001")

    def test_platform_balance_exact_normal_basic_account_and_integer_totals(self):
        row = {"huifu_id": MERCHANT, "acct_id": "BTEST001", "acct_type": "01", "acct_stat": "N",
               "avl_bal": "100.00", "frz_bal": "5.00", "balance_amt": "105.00"}
        def run(rows):
            def respond(url, **kwargs):
                self.assertEqual(url, "https://api.huifu.com/v2/trade/acctpayment/balance/query")
                request = kwargs["json"]["data"]
                return signed_http_response({**request, "resp_code": "00000000", "acctInfo_list": json.dumps(rows)})
            with patch("requests.sessions.Session.post", side_effect=respond):
                return self.gateway.platform_balance("BTEST001")
        self.assertEqual(run([row])["available_amount"], 10000)
        for rows in ([], [row, row], [{**row, "huifu_id": "OTHER"}], [{**row, "acct_type": "03"}],
                     [{**row, "acct_stat": "F"}], [{**row, "acct_id": "OTHER"}],
                     [{**row, "balance_amt": "100.00"}], [{**row, "avl_bal": 100}],
                     [{**row, "avl_bal": "NaN"}], [{**row, "avl_bal": None}]):
            with self.subTest(rows=rows), self.assertRaises(DistributionUncertain):
                run(rows)
