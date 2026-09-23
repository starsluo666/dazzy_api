from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .models import User
from .services import send_sms_code, verify_sms_code


class UserModelTests(TestCase):
    def test_create_user_with_phone(self):
        user = User.objects.create_user(phone="13800000000", password="test-password")
        self.assertEqual(user.phone, "13800000000")
        self.assertTrue(user.check_password("test-password"))
        self.assertEqual(user.verification_status, User.VerificationStatus.UNVERIFIED)


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
        self.assertIsNone(overview.data["data"]["balance_amount"])

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
        self.assertEqual(self.user.account_status, User.AccountStatus.CLOSED)

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
