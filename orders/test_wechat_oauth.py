from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.exceptions import ValidationError

from .wechat_oauth import (
    PAYMENT_RETURN_TARGETS,
    build_payment_authorization,
    complete_payment_authorization,
    get_official_account_openid,
    payment_session_key,
    validate_wechat_payment_payer,
)


@override_settings(
    WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-review",
    WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="test-secret",
    WECHAT_OFFICIAL_ACCOUNT_OAUTH_CALLBACK_URL="https://api.example.test/callback/",
    WECHAT_OFFICIAL_ACCOUNT_H5_PAYMENT_URL="https://app.example.test/#/pages/booking/payment",
)
class WechatPaymentGrantTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.context = dict(user_id=1, order_no="review-order", session_key="browser-A")

    def authorize(self, *, openid="current-wechat", **overrides):
        context = {**self.context, **overrides}
        result = build_payment_authorization(**context, auth_version=2)
        self.assertFalse(result["authorized"])
        state = parse_qs(urlsplit(result["authorize_url"]).query)["state"][0]
        self.assertLessEqual(len(state), 128)
        with (
            patch("orders.wechat_oauth.User.objects.filter") as users,
            patch(
                "orders.wechat_oauth._payment_authorization_business_exists",
                return_value=True,
            ),
            patch("orders.wechat_oauth._exchange_code", return_value=(openid, "union")),
        ):
            users.return_value.first.return_value = SimpleNamespace(pk=context["user_id"])
            return_url = complete_payment_authorization(code="one-use-code", state=state)
            self.assertEqual(users.call_args.kwargs["auth_version"], 2)
        return state, return_url

    def test_no_historical_identity_can_authorize_payment(self):
        # SimpleTestCase disallows DB queries: this must not consult either
        # persistent login bindings or historical payment identities.
        self.assertEqual(get_official_account_openid(user_id=1, app_id="wx-review"), "")
        self.assertFalse(build_payment_authorization(**self.context, auth_version=2)["authorized"])

    def test_all_business_kinds_return_to_their_cashier(self):
        for kind, (path, _parameter) in PAYMENT_RETURN_TARGETS.items():
            with self.subTest(kind=kind):
                _state, return_url = self.authorize(payment_kind=kind)
                self.assertIn(path + "?", return_url)
                self.assertIn("wechatAuthorized=1", return_url)
                self.assertTrue(
                    build_payment_authorization(
                        **self.context,
                        payment_kind=kind,
                        auth_version=2,
                    )["authorized"]
                )

    def test_grant_is_isolated_by_account_app_session_order_and_kind(self):
        self.authorize()
        params = {**self.context, "app_id": "wx-review"}
        self.assertEqual(get_official_account_openid(**params), "current-wechat")
        for overrides in (
            {"user_id": 2},
            {"app_id": "wx-other"},
            {"session_key": "browser-B"},
            {"order_no": "other-order"},
            {"payment_kind": "activity_publish"},
        ):
            self.assertEqual(get_official_account_openid(**{**params, **overrides}), "")

    def test_only_one_concurrent_session_creation_can_consume_grant(self):
        self.authorize()
        params = {**self.context, "app_id": "wx-review", "consume": True}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: get_official_account_openid(**params), range(2)))
        self.assertEqual(sorted(results), ["", "current-wechat"])
        self.assertFalse(build_payment_authorization(**self.context, auth_version=2)["authorized"])

    def test_callback_cannot_be_replayed_or_used_after_expiry(self):
        state, _url = self.authorize()
        with self.assertRaises(ValidationError):
            complete_payment_authorization(code="another-code", state=state)
        cache.clear()
        self.assertEqual(get_official_account_openid(**self.context, app_id="wx-review"), "")
        with self.assertRaises(ValidationError):
            complete_payment_authorization(code="code", state=state)

    def test_changed_login_version_is_rejected_before_code_exchange(self):
        result = build_payment_authorization(**self.context, auth_version=1)
        state = parse_qs(urlsplit(result["authorize_url"]).query)["state"][0]
        with (
            patch("orders.wechat_oauth.User.objects.filter") as users,
            patch(
                "orders.wechat_oauth._exchange_code",
            ) as exchange,
        ):
            users.return_value.first.return_value = None
            with self.assertRaises(ValidationError):
                complete_payment_authorization(code="code", state=state)
            exchange.assert_not_called()

    def test_session_identifier_survives_access_refresh_but_not_new_login(self):
        first = payment_session_key(SimpleNamespace(auth={"session_id": "login-1", "jti": "a"}))
        refreshed = payment_session_key(SimpleNamespace(auth={"session_id": "login-1", "jti": "b"}))
        other = payment_session_key(SimpleNamespace(auth={"session_id": "login-2", "jti": "c"}))
        self.assertEqual(first, refreshed)
        self.assertNotEqual(first, other)
        with self.assertRaises(ValidationError):
            payment_session_key(SimpleNamespace(auth=None))

    def test_preorder_cannot_be_reused_for_another_wechat_or_unknown_legacy_payer(self):
        payment = SimpleNamespace(req_seq_id="", wechat_payer_digest="")
        validate_wechat_payment_payer(payment, trade_type="T_JSAPI", sub_openid="wechat-A")
        self.assertEqual(len(payment.wechat_payer_digest), 64)
        payment.req_seq_id = "original-gateway-request"
        validate_wechat_payment_payer(payment, trade_type="T_JSAPI", sub_openid="wechat-A")
        with self.assertRaises(ValidationError):
            validate_wechat_payment_payer(payment, trade_type="T_JSAPI", sub_openid="wechat-B")
        payment.wechat_payer_digest = ""
        with self.assertRaises(ValidationError):
            validate_wechat_payment_payer(payment, trade_type="T_JSAPI", sub_openid="wechat-A")
        # Non-JSAPI channels do not need browser OAuth and must stay unaffected.
        validate_wechat_payment_payer(payment, trade_type="T_APP", sub_openid="")
