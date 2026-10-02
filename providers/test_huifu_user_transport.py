"""Run the real SDK Request -> signing -> parsing chain, mocking only HTTP."""
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from django.test import SimpleTestCase, override_settings
from requests import Response
from requests.exceptions import Timeout, SSLError

from .huifu_user import ChannelUncertain, HuifuUserGateway, OnboardingUnavailable, UserChannelConfig
from .huifu_user_transport import ensure_transport_ready
from . import test_receiving_onboarding as fixtures


# Distinct ephemeral merchant/channel keypairs prove which key each direction uses.
CHANNEL_KEY = RSA.generate(2048)


def http_response(value, *, status=200):
    response = Response()
    response.status_code = status
    response.encoding = "utf-8"
    response._content = (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)).encode()
    response._content_consumed = True
    return response


def signed_http_response(data, *, key=CHANNEL_KEY):
    # Independent reference signing: ASCII sort ONLY the first level.
    canonical = json.dumps(dict(sorted(data.items())), ensure_ascii=False, separators=(",", ":"))
    signature = pkcs1_15.new(key).sign(SHA256.new(canonical.encode()))
    return http_response({"data": data, "sign": base64.b64encode(signature).decode()})


@override_settings(**{**fixtures.CHANNEL_SETTINGS, "HUIFU_RSA_PUBLIC_KEY": CHANNEL_KEY.public_key().export_key().decode()})
class HuifuUserTransportTests(SimpleTestCase):
    def setUp(self):
        self.config = UserChannelConfig.load(for_submission=True)
        self.gateway = HuifuUserGateway(self.config)
        self.payload = {
            "req_seq_id": "a" * 32, "req_date": "20261002", "name": "测试/达人",
            "cert_type": "00", "cert_no": fixtures.fixtures.synthetic_id(),
            "cert_validity_type": "1", "cert_begin_date": "20200101", "mobile_no": "19900000001",
        }

    def call(self, response, *, kind="register", payload=None):
        with patch("requests.sessions.Session.post", return_value=response):
            return self.gateway.call(kind, payload or self.payload)

    def test_official_request_signature_headers_tls_and_cleanup(self):
        from dg_sdk import DGClient
        from dg_sdk.core.api_request import ApiRequest
        parser = ApiRequest.__dict__["_build_return_data"]
        builder = ApiRequest.__dict__["_build_request_info"]
        previous = {key: getattr(DGClient, key, None) for key in ("env", "mer_config", "connect_timeout")}

        def post(session, url, **kwargs):
            self.assertEqual(url, "https://api.huifu.com/v2/user/basicdata/indv")
            self.assertTrue(session.verify)
            self.assertNotIn("verify", kwargs)
            self.assertEqual(kwargs["timeout"], 15)
            body, headers = kwargs["json"], kwargs["headers"]
            self.assertEqual(body["sys_id"], self.config.sys_id)
            self.assertEqual(body["product_id"], self.config.product_id)
            self.assertEqual(body["data"], self.payload)
            self.assertNotIn("huifu_id", body["data"])
            self.assertEqual(headers["jpt-x-skill-source"], self.config.skill_source)
            self.assertEqual(headers["jpt-x-skill-huifu_id"], "")
            self.assertEqual(headers["jpt-sdk_version"], "python_2.0.24")
            canonical = json.dumps(dict(sorted(body["data"].items())), ensure_ascii=False, separators=(",", ":"))
            pkcs1_15.new(fixtures.KEY.public_key()).verify(SHA256.new(canonical.encode()), base64.b64decode(body["sign"]))
            return signed_http_response({**fixtures.SUCCESS, "req_date": self.payload["req_date"], "req_seq_id": self.payload["req_seq_id"], "login_password": "unused-initial-secret"})

        with patch("requests.sessions.Session.post", autospec=True, side_effect=post) as http:
            result = self.gateway.call("register", self.payload)
        self.assertEqual(result, fixtures.SUCCESS)
        self.assertEqual(http.call_count, 1)
        self.assertIs(ApiRequest.__dict__["_build_return_data"], parser)
        self.assertIs(ApiRequest.__dict__["_build_request_info"], builder)
        self.assertEqual({key: getattr(DGClient, key, None) for key in previous}, previous)

    def test_all_four_requests_use_their_official_routes(self):
        cases = [
            ("register", self.payload, "/v2/user/basicdata/indv"),
            ("configure", {"req_date": "20261002", "req_seq_id": "b" * 32, "huifu_id": fixtures.USER_ID,
                           "upper_huifu_id": self.config.upper_id, "card_info": '{"z":"测试/本人","a":"00"}',
                           "settle_config_list": '[{"settle_cycle":"T1"}]'}, "/v2/user/busi/open"),
            ("query", {"req_date": "20261002", "req_seq_id": "c" * 32, "huifu_id": fixtures.USER_ID}, "/v2/user/basicdata/query"),
            ("recover", {"req_date": "20261002", "req_seq_id": "d" * 32, "legal_cert_no": self.payload["cert_no"], "upper_huifu_id": self.config.upper_id}, "/v2/user/list/query"),
        ]
        for kind, payload, route in cases:
            with self.subTest(kind=kind), patch("requests.sessions.Session.post", return_value=signed_http_response(fixtures.SUCCESS)) as http:
                self.gateway.call(kind, payload)
                self.assertEqual(http.call_args.args[0], "https://api.huifu.com" + route)
                self.assertEqual(http.call_args.kwargs["json"]["data"], payload)

    def test_unsigned_flat_or_wrapped_success_and_error_are_rejected(self):
        for value in (fixtures.SUCCESS, {"data": fixtures.SUCCESS}, {"data": fixtures.SUCCESS, "sign": ""},
                      {"resp_code": "00000001"}, {"data": {"resp_code": "00000001"}}, {"data": fixtures.SUCCESS, "sign": None}):
            with self.subTest(value=list(value)), self.assertRaises(ChannelUncertain):
                self.call(http_response(value))

    def test_tampered_signature_foreign_key_or_data_are_rejected(self):
        signed = json.loads(signed_http_response(fixtures.SUCCESS).text)
        for value in (
            {**signed, "sign": base64.b64encode(b"X" * 256).decode()},
            {**signed, "data": {**fixtures.SUCCESS, "huifu_id": "9000000000000008"}},
            json.loads(signed_http_response(fixtures.SUCCESS, key=fixtures.KEY).text),
        ):
            with self.assertRaises(ChannelUncertain):
                self.call(http_response(value))

    def test_valid_signed_business_failure_is_returned_not_success(self):
        self.assertEqual(self.call(signed_http_response({"resp_code": "00000001"})), {"resp_code": "00000001"})

    def test_invalid_envelopes_http_status_and_oversize_are_uncertain(self):
        sign = json.loads(signed_http_response(fixtures.SUCCESS).text)["sign"]
        malformed = [http_response("<html>gateway error</html>"), http_response([]), http_response({}, status=500),
                     http_response({"data": "{}", "sign": sign}), http_response({"data": [], "sign": sign}),
                     http_response('{"data":{},"data":{},"sign":"' + sign + '"}'),
                     http_response('{"data":{"resp_code":"00000000","huifu_id":NaN},"sign":"' + sign + '"}'),
                     http_response("x" * (1024 * 1024 + 1))]
        for response in malformed:
            with self.assertRaises(ChannelUncertain):
                self.call(response)

    def test_invalid_code_or_mismatched_request_identity_is_uncertain(self):
        for data in ({"resp_code": 0}, {"resp_code": ""}, {**fixtures.SUCCESS, "req_seq_id": "other"},
                     {**fixtures.SUCCESS, "req_date": "20000101"}):
            with self.assertRaises(ChannelUncertain):
                self.call(signed_http_response(data))

    def test_nested_json_strings_chinese_slashes_and_case_preserved(self):
        result = {**fixtures.SUCCESS, "resp_business": '[{"type":"1","msg":"测试/AbC","code":"S"}]', "A": "", "a": {"z": 1, "a": 2}}
        self.assertEqual(self.call(signed_http_response(result))["resp_business"], result["resp_business"])

    def test_network_failure_cleans_up_and_does_not_retry_or_expose_details(self):
        from dg_sdk.core.api_request import ApiRequest
        parser = ApiRequest.__dict__["_build_return_data"]
        for error in (Timeout("secret-body"), SSLError("secret-body")):
            with patch("requests.sessions.Session.post", side_effect=error) as http:
                with self.assertRaises(ChannelUncertain) as raised:
                    self.gateway.call("register", self.payload)
                self.assertNotIn("secret-body", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(http.call_count, 1)
                self.assertIs(ApiRequest.__dict__["_build_return_data"], parser)
        self.assertEqual(self.call(signed_http_response(fixtures.SUCCESS)), fixtures.SUCCESS)

    def test_sdk_unsigned_return_cannot_bypass_the_parser(self):
        with patch("dg_sdk.V2UserBasicdataIndvRequest.post", return_value=fixtures.SUCCESS):
            with self.assertRaises(ChannelUncertain):
                self.gateway.call("register", self.payload)

    def test_request_signing_failure_is_stopped_before_http(self):
        with patch("dg_sdk.core.api_request.rsa_sign", return_value=(False, "invalid-key")), patch("requests.sessions.Session.post") as http:
            with self.assertRaises(ChannelUncertain):
                self.gateway.call("register", self.payload)
            http.assert_not_called()

    def test_verification_cannot_be_disabled_by_sdk(self):
        from dg_sdk.core.api_request import ApiRequest
        def post(*args, **kwargs):
            ApiRequest.need_verfy_sign = False
            return signed_http_response(fixtures.SUCCESS)
        with patch("requests.sessions.Session.post", side_effect=post):
            with self.assertRaises(ChannelUncertain):
                self.gateway.call("register", self.payload)

    def test_unsupported_version_or_sensitive_debug_logs_fail_closed(self):
        for target, value in (("dg_sdk.DGClient.__version__", "2.0.28"), ("dg_sdk.core.log_util.log_level", 20),
                              ("dg_sdk.DGClient.BASE_URL", "http://example.invalid")):
            with patch(target, value), self.assertRaises(OnboardingUnavailable):
                ensure_transport_ready()

    def test_no_sensitive_sdk_logs_on_success_or_signature_failure(self):
        with patch("dg_sdk.core.log_util.logger") as logger:
            self.call(signed_http_response(fixtures.SUCCESS))
            with self.assertRaises(ChannelUncertain):
                self.call(signed_http_response(fixtures.SUCCESS, key=fixtures.KEY))
            self.assertEqual(logger.mock_calls, [])

    def test_concurrent_onboarding_accounts_do_not_mix_keys_or_products(self):
        gateways = [HuifuUserGateway(replace(self.config, product_id=f"OFFLINE_{i}")) for i in range(4)]
        seen = []
        def post(session, url, **kwargs):
            from dg_sdk import DGClient
            from dg_sdk.core.api_request import ApiRequest
            product = kwargs["json"]["product_id"]
            self.assertEqual(DGClient.mer_config.product_id, product)
            self.assertEqual(ApiRequest.product_id, product)
            seen.append(product)
            return signed_http_response(fixtures.SUCCESS)
        with patch("requests.sessions.Session.post", autospec=True, side_effect=post), ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(g.call, "register", self.payload) for g in gateways]
            self.assertTrue(all(f.result(timeout=10) == fixtures.SUCCESS for f in futures))
        self.assertEqual(set(seen), {f"OFFLINE_{i}" for i in range(4)})

    def test_existing_payment_waits_for_guard_then_uses_its_own_configuration(self):
        from dg_sdk.core.api_request import ApiRequest
        from orders.huifu import HuifuAggregatePaymentGateway, HuifuPaymentConfig
        payment = HuifuAggregatePaymentGateway(HuifuPaymentConfig(
            enabled=True, environment="mertest", sys_id="9000000000000011", product_id="PAYMENT_TEST",
            merchant_id="9000000000000012", private_key=self.config.private_key, public_key=self.config.public_key,
            skill_source="hfps/1.3.5", notify_url="https://example.invalid/notify/",
            wechat_official_account_app_id="", wechat_mobile_app_id="", fee_flag="1", connect_timeout_seconds=12,
        ))
        parser_before = ApiRequest.__dict__["_build_return_data"]
        entered, release, payment_started = threading.Event(), threading.Event(), threading.Event()
        def post(session, url, **kwargs):
            if "/v2/user/" in url:
                entered.set()
                self.assertTrue(release.wait(5))
                return signed_http_response(fixtures.SUCCESS)
            self.assertIs(ApiRequest.__dict__["_build_return_data"], parser_before)
            self.assertTrue(url.startswith("https://opps-stbmertest.testpnr.com/"))
            self.assertEqual(kwargs["json"]["product_id"], "PAYMENT_TEST")
            self.assertEqual(kwargs["headers"]["jpt-x-skill-source"], "hfps/1.3.5")
            self.assertEqual(kwargs["timeout"], 12)
            return signed_http_response({"resp_code": "00000000", "huifu_id": payment.merchant_id, "req_date": "20261002", "req_seq_id": "offline-payment", "trans_stat": "S", "trans_amt": "1.00"})
        def query_payment():
            payment_started.set()
            return payment.query_payment(req_date="20261002", req_seq_id="offline-payment")
        with patch("requests.sessions.Session.post", autospec=True, side_effect=post), ThreadPoolExecutor(max_workers=2) as pool:
            opening = pool.submit(self.gateway.call, "register", self.payload)
            try:
                self.assertTrue(entered.wait(5))
                query = pool.submit(query_payment)
                self.assertTrue(payment_started.wait(5))
            finally:
                release.set()
            self.assertEqual(opening.result(timeout=10), fixtures.SUCCESS)
            self.assertEqual(query.result(timeout=10).trans_stat, "S")
