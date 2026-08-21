from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from .models import User


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
