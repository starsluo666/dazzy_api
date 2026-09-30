"""Operator-only registration codes must never become a public production bypass."""

from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework.test import APITestCase

from .models import User
from .services import _code_key


@override_settings(DEBUG=False, SMS_TEST_REGISTRATION_PHONES=("13800000991",))
class TestRegistrationCodeCommandTests(APITestCase):
    phone = "13800000991"

    def setUp(self):
        cache.clear()

    def issue(self, phone=None):
        output = StringIO()
        with patch(
            "accounts.management.commands.issue_test_registration_code.secrets.randbelow",
            return_value=654321,
        ):
            call_command("issue_test_registration_code", phone or self.phone, stdout=output)
        return output.getvalue()

    def test_code_is_random_scoped_to_register_and_used_once(self):
        output = self.issue()
        self.assertIn("654321", output)
        self.assertEqual(cache.get(_code_key(self.phone, "register")), "654321")
        self.assertIsNone(cache.get(_code_key(self.phone, "login")))

        self.assertEqual(self.client.post("/api/v1/auth/register/", {
            "phone": self.phone, "code": "123456", "password": "test-pass-2026",
        }, format="json").status_code, 400)
        response = self.client.post("/api/v1/auth/register/", {
            "phone": self.phone, "code": "654321", "password": "test-pass-2026",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(User.objects.get(phone=self.phone).phone, self.phone)
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    def test_public_sms_endpoint_stays_disabled_in_production(self):
        response = self.client.post("/api/v1/auth/sms-codes/", {
            "phone": self.phone, "purpose": "register",
        }, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    @override_settings(SMS_TEST_REGISTRATION_PHONES=())
    def test_no_allowlist_cannot_issue_code(self):
        with self.assertRaises(CommandError):
            self.issue()
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    def test_other_or_registered_phones_cannot_issue_code(self):
        for invalid in ("13800000992", "123", "not-a-phone"):
            with self.subTest(invalid=invalid), self.assertRaises(CommandError):
                self.issue(invalid)
        User.objects.create_user(phone=self.phone, password="test-pass-2026")
        with self.assertRaises(CommandError):
            self.issue()
        self.assertIsNone(cache.get(_code_key(self.phone, "register")))

    def test_expired_code_cannot_register(self):
        self.issue()
        cache.delete(_code_key(self.phone, "register"))
        response = self.client.post("/api/v1/auth/register/", {
            "phone": self.phone, "code": "654321", "password": "test-pass-2026",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(phone=self.phone).exists())
