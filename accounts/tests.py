import json
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .models import User, WechatLoginIdentity, WechatMiniProgramIdentity, WechatOfficialAccountIdentity
from .services import send_sms_code, verify_sms_code
from .wechat_login import begin_h5_login, complete_h5_callback


@override_settings(
    WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-official-test",
    WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="test-official-secret",
    WECHAT_H5_LOGIN_CALLBACK_URL="https://api.example.test/api/v1/auth/login/wechat/h5/callback/",
    WECHAT_H5_LOGIN_RETURN_URL="https://app.example.test/#/pages/auth/login",
)
class WechatLoginFlowUnitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_h5_state_is_one_use_and_return_is_fixed(self):
        from urllib.parse import parse_qs, urlsplit

        authorize_url, state = begin_h5_login()
        self.assertEqual(parse_qs(urlsplit(authorize_url).query)["state"], [state])
        response = MagicMock()
        response.read.return_value = b'{"openid":"official-user","unionid":""}'
        context = MagicMock()
        context.__enter__.return_value = response
        with patch("accounts.wechat_login.urlopen", return_value=context):
            return_url = complete_h5_callback(code="oauth-code", state=state)
        self.assertTrue(return_url.startswith("https://app.example.test/#/pages/auth/login?"))
        returned = parse_qs(urlsplit(return_url).fragment.split("?", 1)[1])
        self.assertEqual(returned["wechatState"], [state])
        self.assertTrue(returned["wechatTicket"][0])
        with self.assertRaises(ValidationError):
            complete_h5_callback(code="oauth-code", state=state)

    def test_h5_cancel_returns_to_login(self):
        _url, state = begin_h5_login()
        self.assertIn("wechatError=cancelled", complete_h5_callback(code="", state=state))


class SmsDeliverySafetyTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    def test_production_does_not_pretend_to_send_sms_without_gateway(self):
        with self.assertRaises(APIException) as result:
            send_sms_code(phone="13800000001", purpose="wechat_bind")
        self.assertEqual(result.exception.status_code, 503)


class UserModelTests(TestCase):
    def test_create_user_with_phone(self):
        user = User.objects.create_user(phone="13800000000", password="test-password")
        self.assertEqual(user.phone, "13800000000")
        self.assertTrue(user.check_password("test-password"))
        self.assertEqual(user.verification_status, User.VerificationStatus.UNVERIFIED)


@override_settings(
    DEBUG=True,
    SMS_DEVELOPMENT_CODE="123456",
    WECHAT_OFFICIAL_ACCOUNT_APP_ID="wx-official-test",
    WECHAT_OFFICIAL_ACCOUNT_APP_SECRET="test-official-secret",
    WECHAT_MOBILE_APP_ID="wx-mobile-test",
    WECHAT_MOBILE_APP_SECRET="test-mobile-secret",
    WECHAT_H5_LOGIN_CALLBACK_URL="https://api.example.test/api/v1/auth/login/wechat/h5/callback/",
    WECHAT_H5_LOGIN_RETURN_URL="https://app.example.test/#/pages/auth/login",
    WECHAT_CROSS_CHANNEL_UNIONID_ENABLED=True,
)
class WechatWebAndMobileLoginTests(APITestCase):
    def setUp(self):
        cache.clear()

    def mock_exchange(self, *, openid="wx-openid", unionid="wx-unionid"):
        response = MagicMock()
        response.read.return_value = json.dumps({"openid": openid, "unionid": unionid}).encode()
        context = MagicMock()
        context.__enter__.return_value = response
        return patch("accounts.wechat_login.urlopen", return_value=context)

    def mobile_ticket(self, *, openid="wx-openid", unionid="wx-unionid"):
        with self.mock_exchange(openid=openid, unionid=unionid):
            response = self.client.post(
                "/api/v1/auth/login/wechat/mobile/", {"code": "one-use-code"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        return response.data["data"]["ticket"]

    def test_mobile_first_login_binds_existing_phone_and_replay_fails(self):
        user = User.objects.create_user(phone="13800000081", password="existing-password")
        ticket = self.mobile_ticket()
        unresolved = self.client.post(
            "/api/v1/auth/login/wechat/resolve/", {"ticket": ticket}, format="json"
        )
        self.assertEqual(unresolved.data["data"]["status"], "bind_required")
        sent = self.client.post(
            "/api/v1/auth/login/wechat/bind/sms/",
            {"ticket": ticket, "phone": user.phone},
            format="json",
        )
        self.assertEqual(sent.status_code, 200)
        bound = self.client.post(
            "/api/v1/auth/login/wechat/bind/",
            {"ticket": ticket, "phone": user.phone, "code": "123456"},
            format="json",
        )
        self.assertEqual(bound.status_code, 200)
        self.assertFalse(bound.data["data"]["created"])
        self.assertEqual(bound.data["data"]["session"]["user"]["phone"], user.phone)
        self.assertEqual(WechatLoginIdentity.objects.get(openid="wx-openid").user_id, user.pk)
        self.assertEqual(
            self.client.post("/api/v1/auth/login/wechat/resolve/", {"ticket": ticket}, format="json").status_code,
            400,
        )
        another_ticket = self.mobile_ticket()
        logged_in = self.client.post(
            "/api/v1/auth/login/wechat/resolve/", {"ticket": another_ticket}, format="json"
        )
        self.assertEqual(logged_in.data["data"]["status"], "authenticated")
        self.assertEqual(logged_in.data["data"]["session"]["user"]["phone"], user.phone)

    def test_mobile_first_login_creates_account_only_after_verified_phone(self):
        ticket = self.mobile_ticket(openid="fresh", unionid="")
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login/wechat/bind/",
                {"ticket": ticket, "phone": "13800000082", "code": "123456"}, format="json",
            ).status_code,
            400,
        )
        self.client.post(
            "/api/v1/auth/login/wechat/bind/sms/",
            {"ticket": ticket, "phone": "13800000082"}, format="json",
        )
        response = self.client.post(
            "/api/v1/auth/login/wechat/bind/",
            {"ticket": ticket, "phone": "13800000082", "code": "123456"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["created"])
        self.assertFalse(User.objects.get(phone="13800000082").has_usable_password())

    def test_h5_uses_separate_login_identity_and_matches_mobile_unionid(self):
        user = User.objects.create_user(phone="13800000083", password="existing-password")
        WechatLoginIdentity.objects.create(
            user=user, channel="mobile_app", app_id="wx-mobile-test",
            openid="app-openid", unionid="shared-unionid", authorized_at=timezone.now(),
        )
        WechatOfficialAccountIdentity.objects.create(
            user=user, app_id="wx-official-test", openid="payment-openid",
            authorized_at=timezone.now(),
        )
        start = self.client.get("/api/v1/auth/login/wechat/h5/start/")
        self.assertEqual(start.status_code, 200)
        from urllib.parse import parse_qs, urlsplit

        state = parse_qs(urlsplit(start.data["data"]["authorize_url"]).query)["state"][0]
        with self.mock_exchange(openid="h5-openid", unionid="shared-unionid"):
            callback = self.client.get(
                "/api/v1/auth/login/wechat/h5/callback/", {"code": "h5-code", "state": state}
            )
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(
            self.client.get(
                "/api/v1/auth/login/wechat/h5/callback/", {"code": "h5-code", "state": state}
            ).status_code,
            400,
        )
        ticket = parse_qs(urlsplit(callback["Location"]).fragment.split("?", 1)[1])["wechatTicket"][0]
        resolved = self.client.post(
            "/api/v1/auth/login/wechat/resolve/", {"ticket": ticket}, format="json"
        )
        self.assertEqual(resolved.data["data"]["status"], "authenticated")
        self.assertEqual(WechatLoginIdentity.objects.get(app_id="wx-official-test").user_id, user.pk)
        self.assertEqual(
            WechatOfficialAccountIdentity.objects.get(user=user).openid, "payment-openid"
        )

    def test_blocked_account_cannot_use_wechat_login(self):
        user = User.objects.create_user(phone="13800000084", password="existing-password")
        user.account_status = User.AccountStatus.SUSPENDED
        user.save(update_fields=("account_status",))
        WechatLoginIdentity.objects.create(
            user=user, channel="mobile_app", app_id="wx-mobile-test",
            openid="blocked-openid", authorized_at=timezone.now(),
        )
        ticket = self.mobile_ticket(openid="blocked-openid", unionid="")
        response = self.client.post(
            "/api/v1/auth/login/wechat/resolve/", {"ticket": ticket}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_h5_login_binding_does_not_supply_current_payment_openid(self):
        from orders.wechat_oauth import get_official_account_openid

        user = User.objects.create_user(phone="13800000085", password="existing-password")
        WechatLoginIdentity.objects.create(
            user=user, channel="official_account", app_id="wx-official-test",
            openid="login-openid", authorized_at=timezone.now(),
        )
        self.assertEqual(
            get_official_account_openid(user_id=user.pk, app_id="wx-official-test"),
            "",
        )
        self.assertFalse(WechatOfficialAccountIdentity.objects.filter(user=user).exists())


@override_settings(DEBUG=True, SMS_DEVELOPMENT_CODE="123456")
class AuthenticationApiTests(APITestCase):
    phone = "13800000001"
    password = "test-pass-123"

    def setUp(self):
        cache.clear()

    def request_code(self, purpose: str, phone: str | None = None):
        return self.client.post(
            "/api/v1/auth/sms-codes/",
            {"phone": phone or self.phone, "purpose": purpose},
            format="json",
        )

    def test_register_returns_tokens_and_current_user(self):
        code_response = self.request_code("register")
        self.assertEqual(code_response.status_code, 200)
        self.assertEqual(code_response.data["data"]["debug_code"], "123456")

        response = self.client.post(
            "/api/v1/auth/register/",
            {"phone": self.phone, "code": "123456", "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("access", response.data["data"])
        self.assertIn("refresh", response.data["data"])
        self.assertEqual(response.data["data"]["user"]["phone"], self.phone)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['data']['access']}")
        me_response = self.client.get("/api/v1/users/me/")
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.data["data"]["phone"], self.phone)
        overview = self.client.get("/api/v1/users/me/overview/")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.data["data"]["order_count"], 0)
        self.assertEqual(overview.data["data"]["pending_review_count"], 0)
        self.assertEqual(overview.data["data"]["customer_service_phone"], "")
        self.assertEqual(overview.data["data"]["balance_amount"], 0)

    def test_password_and_sms_login(self):
        User.objects.create_user(phone=self.phone, password=self.password)
        password_response = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        )
        self.assertEqual(password_response.status_code, 200)

        self.assertEqual(self.request_code("login").status_code, 200)
        sms_response = self.client.post(
            "/api/v1/auth/login/sms/",
            {"phone": self.phone, "code": "123456"},
            format="json",
        )
        self.assertEqual(sms_response.status_code, 200)

    def test_current_user_can_update_profile(self):
        user = User.objects.create_user(phone=self.phone, password=self.password, nickname="旧昵称")
        self.client.force_authenticate(user)

        response = self.client.patch(
            "/api/v1/users/me/",
            {"nickname": "  新昵称  ", "gender": "female", "birth_date": "2000-05-20"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["nickname"], "新昵称")
        self.assertEqual(response.data["data"]["gender"], "female")
        self.assertEqual(response.data["data"]["birth_date"], "2000-05-20")

    def test_current_user_cannot_set_future_birth_date(self):
        user = User.objects.create_user(phone=self.phone, password=self.password)
        self.client.force_authenticate(user)

        response = self.client.patch(
            "/api/v1/users/me/", {"birth_date": "2999-01-01"}, format="json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("birth_date", response.data)

    def test_reset_password_consumes_code(self):
        user = User.objects.create_user(phone=self.phone, password=self.password)
        self.assertEqual(self.request_code("reset_password").status_code, 200)
        response = self.client.post(
            "/api/v1/auth/password/reset/",
            {"phone": self.phone, "code": "123456", "new_password": "new-pass-456"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("new-pass-456"))

        reused = self.client.post(
            "/api/v1/auth/password/reset/",
            {"phone": self.phone, "code": "123456", "new_password": "other-pass-789"},
            format="json",
        )
        self.assertEqual(reused.status_code, 400)

    def test_authenticated_user_can_view_account_security_status(self):
        user = User.objects.create_user(phone=self.phone, password=self.password)
        self.client.force_authenticate(user)

        response = self.client.get("/api/v1/auth/security/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["phone_masked"], "138****0001")
        self.assertTrue(response.data["data"]["password_set"])
        self.assertEqual(response.data["data"]["account_status"], "active")
        self.assertEqual(response.data["data"]["account_status_label"], "正常")

    @override_settings(SMS_CODE_RESEND_SECONDS=0)
    def test_authenticated_user_can_change_phone_with_two_sms_codes(self):
        user = User.objects.create_user(phone=self.phone, password=self.password)
        self.client.force_authenticate(user)

        current_code = self.client.post(
            "/api/v1/auth/phone/change/code/",
            {"target": "current"},
            format="json",
        )
        new_code = self.client.post(
            "/api/v1/auth/phone/change/code/",
            {"target": "new", "new_phone": "13900000002"},
            format="json",
        )
        self.assertEqual(current_code.status_code, 200)
        self.assertEqual(new_code.status_code, 200)

        response = self.client.post(
            "/api/v1/auth/phone/change/",
            {
                "current_code": "123456",
                "new_phone": "13900000002",
                "new_code": "123456",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["user"]["phone"], "13900000002")
        user.refresh_from_db()
        self.assertEqual(user.phone, "13900000002")
        self.assertEqual(user.auth_version, 2)

    def test_change_phone_rejects_phone_bound_to_another_account(self):
        user = User.objects.create_user(phone=self.phone, password=self.password)
        User.objects.create_user(phone="13900000003", password=self.password)
        self.client.force_authenticate(user)

        response = self.client.post(
            "/api/v1/auth/phone/change/code/",
            {"target": "new", "new_phone": "13900000003"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("已绑定其他账号", str(response.data))

    def test_change_password_rotates_current_session_and_invalidates_old_tokens(self):
        User.objects.create_user(phone=self.phone, password=self.password)
        login = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        ).data["data"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']}")

        changed = self.client.post(
            "/api/v1/auth/password/change/",
            {"current_password": self.password, "new_password": "new-secure-pass-456"},
            format="json",
        )

        self.assertEqual(changed.status_code, 200)
        self.assertIn("access", changed.data["data"])
        self.assertIn("refresh", changed.data["data"])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']}")
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 401)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {changed.data['data']['access']}"
        )
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 200)

        self.client.credentials()
        old_login = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        )
        new_login = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": "new-secure-pass-456"},
            format="json",
        )
        self.assertEqual(old_login.status_code, 400)
        self.assertEqual(new_login.status_code, 200)

    def test_logout_other_sessions_requires_password_and_rotates_tokens(self):
        User.objects.create_user(phone=self.phone, password=self.password)
        first = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        ).data["data"]
        second = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        ).data["data"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {first['access']}")

        rejected = self.client.post(
            "/api/v1/auth/sessions/logout-others/",
            {"current_password": "incorrect-password"},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)

        rotated = self.client.post(
            "/api/v1/auth/sessions/logout-others/",
            {"current_password": self.password},
            format="json",
        )
        self.assertEqual(rotated.status_code, 200)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {second['access']}")
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 401)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {rotated.data['data']['access']}"
        )
        self.assertEqual(self.client.get("/api/v1/auth/security/").status_code, 200)

        self.client.credentials()
        old_refresh = self.client.post(
            "/api/v1/auth/token/refresh/", {"refresh": second["refresh"]}, format="json"
        )
        self.assertEqual(old_refresh.status_code, 401)

    def test_rejects_duplicate_registration_and_fast_resend(self):
        User.objects.create_user(phone=self.phone, password=self.password)
        duplicate = self.request_code("register")
        self.assertEqual(duplicate.status_code, 400)

        first = self.request_code("login")
        second = self.request_code("login")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_password_login_locks_after_repeated_failures(self):
        User.objects.create_user(phone=self.phone, password=self.password)
        for _ in range(5):
            response = self.client.post(
                "/api/v1/auth/login/password/",
                {"phone": self.phone, "password": "wrong-password"},
                format="json",
            )
            self.assertEqual(response.status_code, 400)

        locked = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
        )
        self.assertEqual(locked.status_code, 400)
        self.assertIn("尝试次数过多", str(locked.data))

    def test_failures_from_one_ip_do_not_lock_phone_on_another_ip(self):
        User.objects.create_user(phone=self.phone, password=self.password)
        for _ in range(5):
            response = self.client.post(
                "/api/v1/auth/login/password/",
                {"phone": self.phone, "password": "wrong-password"},
                format="json",
                REMOTE_ADDR="10.0.0.1",
            )
            self.assertEqual(response.status_code, 400)

        allowed = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": self.phone, "password": self.password},
            format="json",
            REMOTE_ADDR="10.0.0.2",
        )

        self.assertEqual(allowed.status_code, 200)

    @override_settings(AUTH_IP_FAILURE_LIMIT=3)
    def test_ip_is_locked_after_failures_against_multiple_phones(self):
        phones = [f"1380000020{index}" for index in range(4)]
        for phone in phones:
            User.objects.create_user(phone=phone, password=self.password)
        for phone in phones[:3]:
            response = self.client.post(
                "/api/v1/auth/login/password/",
                {"phone": phone, "password": "wrong-password"},
                format="json",
                REMOTE_ADDR="10.0.0.3",
            )
            self.assertEqual(response.status_code, 400)

        locked = self.client.post(
            "/api/v1/auth/login/password/",
            {"phone": phones[3], "password": self.password},
            format="json",
            REMOTE_ADDR="10.0.0.3",
        )

        self.assertEqual(locked.status_code, 400)
        self.assertIn("当前网络登录失败次数过多", str(locked.data))

    @override_settings(SMS_PHONE_DAILY_LIMIT=2, SMS_CODE_RESEND_SECONDS=0)
    def test_sms_phone_daily_limit_spans_purposes(self):
        User.objects.create_user(phone=self.phone, password=self.password)

        self.assertEqual(self.request_code("login").status_code, 200)
        self.assertEqual(self.request_code("reset_password").status_code, 200)
        limited = self.request_code("login")

        self.assertEqual(limited.status_code, 400)
        self.assertIn("今日获取验证码次数已达上限", str(limited.data))

    @override_settings(SMS_CODE_RESEND_SECONDS=0, SMS_PHONE_DAILY_LIMIT=100)
    @patch("config.throttles.SmsSendIpDailyThrottle.rate", "2/day", create=True)
    def test_sms_send_ip_daily_limit_spans_phone_numbers(self):
        for index in range(2):
            response = self.request_code("register", f"1380000030{index}")
            self.assertEqual(response.status_code, 200)

        limited = self.request_code("register", "13800000302")

        self.assertEqual(limited.status_code, 429)

    @override_settings(SMS_CODE_MAX_ATTEMPTS=3)
    def test_sms_code_is_invalidated_after_maximum_failed_attempts(self):
        send_sms_code(phone=self.phone, purpose="register")

        for _ in range(2):
            with self.assertRaisesMessage(ValidationError, "验证码错误或已过期"):
                verify_sms_code(phone=self.phone, purpose="register", code="000000")
        with self.assertRaisesMessage(ValidationError, "验证码尝试次数过多"):
            verify_sms_code(phone=self.phone, purpose="register", code="000000")
        with self.assertRaisesMessage(ValidationError, "验证码错误或已过期"):
            verify_sms_code(phone=self.phone, purpose="register", code="123456")

    def test_register_endpoint_is_rate_limited_by_ip(self):
        for index in range(5):
            response = self.client.post(
                "/api/v1/auth/register/",
                {
                    "phone": f"138000001{index:02d}",
                    "code": "000000",
                    "password": self.password,
                },
                format="json",
            )
            self.assertEqual(response.status_code, 400)

        limited = self.client.post(
            "/api/v1/auth/register/",
            {"phone": "13800000199", "code": "000000", "password": self.password},
            format="json",
        )
        self.assertEqual(limited.status_code, 429)


@override_settings(
    WECHAT_CUSTOMER_MINI_PROGRAM_APP_ID="wx-customer-test",
    WECHAT_CUSTOMER_MINI_PROGRAM_APP_SECRET="customer-secret",
    WECHAT_PROVIDER_MINI_PROGRAM_APP_ID="wx-provider-test",
    WECHAT_PROVIDER_MINI_PROGRAM_APP_SECRET="provider-secret",
)
class WechatMiniProgramLoginApiTests(APITestCase):
    @patch("accounts.serializers.exchange_phone_code", return_value="13800000888")
    @patch(
        "accounts.serializers.exchange_login_code",
        return_value=("customer-openid", "shared-unionid"),
    )
    def test_first_login_links_existing_phone_account(self, _login, _phone):
        user = User.objects.create_user(phone="13800000888", password="test-pass-123")

        response = self.client.post(
            "/api/v1/auth/login/wechat-mini-program/",
            {
                "client_type": "customer",
                "login_code": "login-code",
                "phone_code": "phone-code",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["user"]["public_id"], str(user.public_id))
        identity = WechatMiniProgramIdentity.objects.get(user=user)
        self.assertEqual(identity.app_id, "wx-customer-test")
        self.assertEqual(identity.openid, "customer-openid")

    @patch("accounts.serializers.exchange_phone_code", return_value="13900000888")
    @patch(
        "accounts.serializers.exchange_login_code",
        return_value=("new-openid", "new-unionid"),
    )
    def test_first_login_creates_passwordless_account(self, _login, _phone):
        response = self.client.post(
            "/api/v1/auth/login/wechat-mini-program/",
            {
                "client_type": "customer",
                "login_code": "login-code",
                "phone_code": "phone-code",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(phone="13900000888")
        self.assertFalse(user.has_usable_password())
        self.assertTrue(
            WechatMiniProgramIdentity.objects.filter(
                user=user, app_id="wx-customer-test", openid="new-openid"
            ).exists()
        )

    @patch("accounts.serializers.exchange_phone_code")
    @patch(
        "accounts.serializers.exchange_login_code",
        return_value=("bound-openid", "bound-unionid"),
    )
    def test_bound_wechat_identity_does_not_require_phone_authorization(
        self, _login, phone_exchange
    ):
        user = User.objects.create_user(phone="13700000888", password=None)
        WechatMiniProgramIdentity.objects.create(
            user=user,
            app_id="wx-customer-test",
            openid="bound-openid",
            authorized_at=timezone.now(),
        )

        response = self.client.post(
            "/api/v1/auth/login/wechat-mini-program/",
            {"client_type": "customer", "login_code": "login-code"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        phone_exchange.assert_not_called()


class AccountClosureApiTests(APITestCase):
    password = "test-pass-123"

    def setUp(self):
        self.user = User.objects.create_user(
            phone="13800000999",
            password=self.password,
        )
        self.client.force_authenticate(self.user)

    def test_account_without_unresolved_business_can_close(self):
        response = self.client.post(
            "/api/v1/auth/account/close/",
            {"current_password": self.password},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSURE_PENDING)
        self.assertFalse(response.data["data"]["closed"])
        self.assertEqual(response.data["data"]["working_days"], 5)

    def test_open_support_case_blocks_account_closure_with_actionable_reason(self):
        from supportcases.models import SupportCase

        SupportCase.objects.create(
            reporter=self.user,
            case_type=SupportCase.CaseType.CONSULTATION,
            target_type=SupportCase.TargetType.GENERAL,
            reason=SupportCase.Reason.ACCOUNT_ISSUE,
            description="仍在处理中的账号问题",
        )

        response = self.client.post(
            "/api/v1/auth/account/close/",
            {"current_password": self.password},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("未结业务", str(response.data["business"]))
        self.assertEqual(response.data["blocking_items"][0]["code"], "support_cases")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.account_status, User.AccountStatus.ACTIVE)
