import base64
import hashlib
import hmac
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from backoffice.serializers import ProviderAdminSerializer
from .models import ProviderProfile, ProviderReceivingAccount
from .receiving_accounts import CONSENT_VERSION, decrypt_details


TEST_KEY = base64.b64encode(b"T" * 32).decode()  # Isolated test fixture, never a deployment default.
URL = "/api/v1/providers/me/receiving-account/"


def synthetic_id(prefix="11010119900101001"):
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    return prefix + "10X98765432"[sum(int(n) * w for n, w in zip(prefix, weights)) % 11]


@override_settings(
    PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED=True,
    PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=TEST_KEY,
)
class ReceivingAccountTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(phone="19900000001", password="offline-test-only")
        self.provider = ProviderProfile.objects.create(
            user=self.user, status="approved", identity_status="verified",
            identity_real_name="测试达人", application_real_name="测试达人",
            identity_number_digest=hmac.new(
                settings.SECRET_KEY.encode(), synthetic_id().encode(), hashlib.sha256
            ).hexdigest(),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.payload = {
            "id_number": synthetic_id(), "cert_begin_date": "2020-01-01",
            "cert_end_date": "2040-01-01", "cert_long_term": False,
            "mobile": "19900000001", "bank_card_number": "6222000000000000",
            "bank_name": "测试银行", "bank_province": "河北省", "bank_city": "邯郸市",
            "consent_accepted": True, "consent_version": CONSENT_VERSION,
        }

    def save(self, **changes):
        return self.client.put(URL, {**self.payload, **changes}, format="json")

    def test_auth_required_for_all_operations(self):
        self.client.force_authenticate(None)
        for method in (self.client.get, self.client.put, self.client.delete):
            self.assertEqual(method(URL).status_code, 401)

    def test_unverified_cannot_save_and_get_does_not_create_record(self):
        self.provider.identity_status = "pending"
        self.provider.save()
        response = self.client.get(URL)
        self.assertFalse(response.data["data"]["identity_verified"])
        self.assertEqual(self.save().status_code, 403)
        self.assertFalse(ProviderReceivingAccount.objects.exists())

    def test_disabled_and_missing_key_fail_closed(self):
        for override in (
            {"PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED": False},
            {"PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY": ""},
            {"PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY": "invalid"},
        ):
            with self.settings(**override):
                self.assertFalse(self.client.get(URL).data["data"]["collection_enabled"])
                self.assertEqual(self.save().status_code, 503)
        self.assertFalse(ProviderReceivingAccount.objects.exists())

    def test_encrypted_storage_masked_response_and_no_channel_call(self):
        with patch("dg_sdk.V2UserBasicdataIndvRequest.post") as register:
            response = self.save()
        register.assert_not_called()
        self.assertEqual(response.status_code, 200, response.data)
        record = ProviderReceivingAccount.objects.get()
        decrypted = decrypt_details(record)
        for field in ("id_number", "bank_card_number", "mobile"):
            self.assertEqual(decrypted[field], self.payload[field])
            self.assertNotIn(self.payload[field], response.content.decode())
            self.assertNotIn(self.payload[field], record.details_ciphertext)
        self.assertEqual(response.data["data"]["channel_status"], "not_connected")
        self.assertIn("待渠道开通", response.data["data"]["status_label"])
        self.assertIn("no-store", response["Cache-Control"])
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.identity_status, "verified")
        self.assertFalse(self.provider.is_accepting_orders)

    def test_edits_retain_masked_fields_and_do_not_duplicate(self):
        self.assertEqual(self.save().status_code, 200)
        first_ciphertext = ProviderReceivingAccount.objects.get().details_ciphertext
        response = self.save(id_number="", bank_card_number="", mobile="", bank_city="石家庄市")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ProviderReceivingAccount.objects.count(), 1)
        record = ProviderReceivingAccount.objects.get()
        self.assertNotEqual(record.details_ciphertext, first_ciphertext)
        self.assertEqual(decrypt_details(record)["bank_card_number"], self.payload["bank_card_number"])
        self.assertEqual(record.bank_city, "石家庄市")

    def test_different_identity_or_invalid_checksum_rejected(self):
        response = self.save(id_number=synthetic_id("11010119900201001"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("不一致", str(response.data))
        response = self.save(id_number="110101199002010000")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ProviderReceivingAccount.objects.exists())

    def test_expired_future_and_missing_validity_rejected(self):
        for values in (
            {"cert_end_date": "2020-01-01"}, {"cert_end_date": None},
            {"cert_begin_date": "2099-01-01"},
        ):
            self.assertEqual(self.save(**values).status_code, 400)
        self.assertFalse(ProviderReceivingAccount.objects.exists())

    def test_long_term_and_consent(self):
        self.assertEqual(self.save(consent_accepted=False).status_code, 400)
        self.assertEqual(self.save(consent_version="old").status_code, 400)
        self.assertFalse(ProviderReceivingAccount.objects.exists())
        self.assertEqual(self.save(cert_long_term=True, cert_end_date=None).status_code, 200)
        self.assertIsNone(self.client.get(URL).data["data"]["cert_end_date"])

    def test_owner_is_never_taken_from_input(self):
        other = User.objects.create_user(phone="19900000002", password="offline-test-only")
        other_provider = ProviderProfile.objects.create(user=other, status="approved")
        response = self.save(provider_id=other_provider.pk, channel_status="active")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProviderReceivingAccount.objects.get().provider_id, self.provider.pk)
        self.client.force_authenticate(other)
        self.assertFalse(self.client.get(URL).data["data"]["materials_saved"])
        self.assertEqual(self.client.delete(URL).status_code, 204)
        self.assertTrue(ProviderReceivingAccount.objects.filter(provider=self.provider).exists())

    def test_tampered_ciphertext_or_wrong_key_is_not_overwritten(self):
        self.assertEqual(self.save().status_code, 200)
        record = ProviderReceivingAccount.objects.get()
        original = record.details_ciphertext
        with self.settings(PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=base64.b64encode(b"x" * 32).decode()):
            response = self.client.get(URL)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.data["data"]["collection_enabled"])
            self.assertTrue(response.data["data"]["materials_saved"])
            self.assertIn("无法读取", response.data["data"]["collection_unavailable_reason"])
            self.assertEqual(self.save().status_code, 503)
        record.refresh_from_db()
        self.assertEqual(record.details_ciphertext, original)
        record.details_ciphertext = "v1:" + base64.b64encode(b"x" * 50).decode()
        record.save()
        self.assertFalse(self.client.get(URL).data["data"]["collection_enabled"])

    def test_ciphertext_is_bound_to_provider(self):
        self.assertEqual(self.save().status_code, 200)
        record = ProviderReceivingAccount.objects.get()
        record.provider_id += 1
        from .receiving_accounts import ReceivingAccountUnavailable
        with self.assertRaises(ReceivingAccountUnavailable):
            decrypt_details(record)

    def test_clear_allowed_with_collection_disabled_or_key_unavailable(self):
        self.assertEqual(self.save().status_code, 200)
        with self.settings(PROVIDER_RECEIVING_ACCOUNT_COLLECTION_ENABLED=False, PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=""):
            self.assertEqual(self.client.delete(URL).status_code, 204)
            self.assertEqual(self.client.delete(URL).status_code, 204)
        self.assertFalse(ProviderReceivingAccount.objects.exists())

    def test_staff_summary_requires_review_permission_and_detail_scope(self):
        self.assertEqual(self.save().status_code, 200)
        for context in ({}, {"can_review": True}, {"include_detail": True}):
            serializer = ProviderAdminSerializer(context=context)
            self.assertIsNone(serializer.get_receiving_account(self.provider))
        serializer = ProviderAdminSerializer(context={"can_review": True, "include_detail": True})
        result = serializer.get_receiving_account(self.provider)
        self.assertEqual(result["bank_card_masked"], "**** **** **** 0000")
        self.assertNotIn("details_ciphertext", result)
        self.assertNotIn(self.payload["bank_card_number"], str(result))

    def test_closed_user_cannot_recreate_materials(self):
        self.user.account_status = "closed"
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.save().status_code, 403)
        self.assertFalse(ProviderReceivingAccount.objects.exists())

    def test_account_closure_clears_unsubmitted_materials(self):
        from accounts.account_closure import process_account_closure, request_account_closure
        self.assertEqual(self.save().status_code, 200)
        closure = request_account_closure(self.user, "offline-test-only")
        self.assertEqual(process_account_closure(self.user.pk, now=closure.execute_after), "completed")
        self.assertFalse(ProviderReceivingAccount.objects.exists())
