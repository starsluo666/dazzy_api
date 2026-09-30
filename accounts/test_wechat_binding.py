from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import User, WechatLoginIdentity, WechatOfficialAccountIdentity
from .views import auth_payload
from .wechat_binding import STATE_PREFIX, TICKET_PREFIX
from .wechat_login import WechatIdentity, _attach_identity, _issue_ticket


@override_settings(
    WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-binding-h5",
    WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="test-only-secret",
    WECHAT_MOBILE_APP_ID="wx-binding-app",
    WECHAT_MOBILE_APP_SECRET="test-only-secret",
    WECHAT_H5_LOGIN_CALLBACK_URL="https://api.example.invalid/api/v1/auth/login/wechat/h5/callback/",
    WECHAT_H5_LOGIN_RETURN_URL="https://h5.example.invalid/#/pages/auth/login",
    WECHAT_CROSS_CHANNEL_UNIONID_ENABLED=True,
)
class WechatBindingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="13800000801", password="binding-pass-2026")
        self.other = User.objects.create_user(phone="13800000802", password="binding-pass-2026")
        self.h5 = WechatIdentity("official_account", "wx-binding-h5", "openid-h5", "union-binding")
        self.app = WechatIdentity("mobile_app", "wx-binding-app", "openid-app", "union-binding")
        self.session = auth_payload(self.user)
        self.authenticate(self.session)

    def authenticate(self, session):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {session['access']}")

    def start(self):
        response = self.client.post("/api/v1/auth/wechat/binding/h5/start/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]["state"]

    def callback(self, state, code="test-code"):
        with patch("accounts.wechat_binding._exchange_code", return_value=self.h5) as exchange:
            response = self.client.get("/api/v1/auth/login/wechat/h5/callback/", {"state": state, "code": code})
        self.assertEqual(response.status_code, 302, response.data if hasattr(response, "data") else response.content)
        query = parse_qs(urlsplit(response.url).fragment.split("?", 1)[1])
        self.assertEqual(query["wechatBindState"], [state])
        return response, query, exchange

    def complete(self, ticket):
        return self.client.post("/api/v1/auth/wechat/binding/h5/complete/", {"ticket": ticket}, format="json")

    def test_binding_auth_required_and_status_has_no_private_identity(self):
        self.assertEqual(self.client.get("/api/v1/auth/wechat/binding/").data["data"], {"bound": False, "channels": []})
        self.client.credentials()
        for path in ("/api/v1/auth/wechat/binding/h5/start/", "/api/v1/auth/wechat/binding/h5/complete/", "/api/v1/auth/wechat/binding/mobile/"):
            self.assertEqual(self.client.post(path, {}, format="json").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/auth/wechat/binding/").status_code, 401)

    def test_h5_binds_only_current_account_no_registration_or_session_switch(self):
        state = self.start()
        owner = cache.get(f"{STATE_PREFIX}{state}")
        self.assertEqual(owner["user_id"], self.user.pk)
        response, query, _exchange = self.callback(state)
        self.assertTrue(response.url.startswith("https://h5.example.invalid/#/pages/profile/edit?"))
        self.assertFalse(WechatLoginIdentity.objects.exists(), "callback alone must not bind")
        ticket = query["wechatBindTicket"][0]
        result = self.complete(ticket)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["data"], {"bound": True, "channels": ["official_account"]})
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(WechatLoginIdentity.objects.get().user_id, self.user.pk)
        self.assertEqual(self.client.get("/api/v1/users/me/").data["data"]["public_id"], str(self.user.public_id))
        self.assertEqual(self.complete(ticket).status_code, 400)

    def test_cancel_does_not_exchange_code_or_bind(self):
        _response, query, exchange = self.callback(self.start(), code="")
        self.assertEqual(query["wechatBindError"], ["cancelled"])
        self.assertNotIn("wechatBindTicket", query)
        exchange.assert_not_called()
        self.assertFalse(WechatLoginIdentity.objects.exists())

    def test_callback_state_cannot_be_replayed_or_omitted(self):
        state = self.start()
        self.callback(state)
        for invalid in (state, "bind_expired", ""):
            with patch("accounts.wechat_binding._exchange_code") as exchange:
                response = self.client.get("/api/v1/auth/login/wechat/h5/callback/", {"state": invalid, "code": "test-code"})
            self.assertEqual(response.status_code, 400)
            exchange.assert_not_called()

    def test_binding_ticket_cannot_be_used_for_login_or_by_other_account(self):
        _response, query, _exchange = self.callback(self.start())
        ticket = query["wechatBindTicket"][0]
        response = self.client.post("/api/v1/auth/login/wechat/resolve/", {"ticket": ticket}, format="json")
        self.assertEqual(response.status_code, 400)
        self.authenticate(auth_payload(self.other))
        self.assertEqual(self.complete(ticket).status_code, 400)
        self.assertFalse(WechatLoginIdentity.objects.exists())
        self.authenticate(self.session)
        self.assertEqual(self.complete(ticket).status_code, 200)

    def test_login_ticket_cannot_link_authenticated_account(self):
        self.assertEqual(self.complete(_issue_ticket(self.h5)).status_code, 400)
        self.assertFalse(WechatLoginIdentity.objects.exists())

    def test_binding_ticket_is_specific_to_authorizing_session(self):
        _response, query, _exchange = self.callback(self.start())
        self.authenticate(auth_payload(self.user))
        self.assertEqual(self.complete(query["wechatBindTicket"][0]).status_code, 400)
        self.assertFalse(WechatLoginIdentity.objects.exists())

    def test_django_sessions_are_also_isolated(self):
        self.client.credentials()
        self.client.force_login(self.user)
        _response, query, _exchange = self.callback(self.start())
        self.client.logout()
        self.client.force_login(self.user)
        self.assertEqual(self.complete(query["wechatBindTicket"][0]).status_code, 400)
        self.assertFalse(WechatLoginIdentity.objects.exists())

    def test_expired_ticket_or_changed_configuration_cannot_bind(self):
        _response, query, _exchange = self.callback(self.start())
        ticket = query["wechatBindTicket"][0]
        with override_settings(WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-changed"):
            self.assertEqual(self.complete(ticket).status_code, 400)
        cache.delete(f"{TICKET_PREFIX}{ticket}")
        self.assertEqual(self.complete(ticket).status_code, 400)
        self.assertFalse(WechatLoginIdentity.objects.exists())

    def test_other_owner_and_existing_different_wechat_are_not_overwritten(self):
        _attach_identity(identity=self.h5, user=self.other)
        _response, query, _exchange = self.callback(self.start())
        self.assertEqual(self.complete(query["wechatBindTicket"][0]).status_code, 400)
        self.assertEqual(WechatLoginIdentity.objects.get().user_id, self.other.pk)
        with patch("accounts.wechat_binding._exchange_code", return_value=self.app):
            response = self.client.post("/api/v1/auth/wechat/binding/mobile/", {"code": "test-code"}, format="json")
        self.assertEqual(response.status_code, 400, "UnionID ownership must also prevent cross-channel takeover")
        self.assertFalse(WechatLoginIdentity.objects.filter(user=self.user).exists())

    def test_mobile_binding_keeps_account_and_is_idempotent_for_same_identity(self):
        with patch("accounts.wechat_binding._exchange_code", return_value=self.app):
            for _ in range(2):
                response = self.client.post("/api/v1/auth/wechat/binding/mobile/", {"code": "test-code", "user_id": self.other.pk, "openid": "forged"}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(WechatLoginIdentity.objects.count(), 1)
        self.assertEqual(WechatLoginIdentity.objects.get().user_id, self.user.pk)
        self.assertEqual(User.objects.count(), 2)
        different = WechatIdentity("mobile_app", "wx-binding-app", "another-openid", "another-union")
        with patch("accounts.wechat_binding._exchange_code", return_value=different):
            response = self.client.post("/api/v1/auth/wechat/binding/mobile/", {"code": "new-code"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(WechatLoginIdentity.objects.get().openid, self.app.openid)

    def test_payment_identity_does_not_falsely_show_login_binding(self):
        WechatOfficialAccountIdentity.objects.create(user=self.user, app_id="wx-binding-h5", openid="payment-openid", authorized_at=timezone.now())
        self.assertFalse(self.client.get("/api/v1/auth/wechat/binding/").data["data"]["bound"])

    def test_security_change_during_code_exchange_blocks_mobile_binding(self):
        def change_security(**kwargs):
            User.objects.filter(pk=self.user.pk).update(auth_version=self.user.auth_version + 1)
            return self.app
        with patch("accounts.wechat_binding._exchange_code", side_effect=change_security):
            response = self.client.post("/api/v1/auth/wechat/binding/mobile/", {"code": "test-code"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WechatLoginIdentity.objects.exists())

    @override_settings(WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="")
    def test_missing_configuration_never_creates_identity(self):
        self.assertEqual(self.client.post("/api/v1/auth/wechat/binding/h5/start/", {}, format="json").status_code, 503)
        self.assertFalse(WechatLoginIdentity.objects.exists())
