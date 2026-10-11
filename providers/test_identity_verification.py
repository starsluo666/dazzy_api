"""Offline synthetic fixtures; never send ID photos or incur Tencent charges."""
import base64
from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import json
from io import BytesIO
import logging
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient
from requests import Response
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException

from accounts.models import User
from mediafiles.models import MediaAsset
from .identity_verification import (
    CONSENT_VERSION, IdentityVerificationUnavailable, fingerprint, require_verified_check,
    verification_summary,
)
from .models import (ProviderIdentityCheck, ProviderProfile, ProviderProfileRevision,
                     ProviderServiceRevision, ServiceCategory)
from .services import save_provider_identity, submit_provider_identity
from .tencent_identity import CheckResult, check_identity_photo, encode_image, read_identity_image

NUMBER = "110101199001010015"  # Synthetic fixture, not a real user's supplied credentials.


def digest():
    return hmac.new(settings.SECRET_KEY.encode(), NUMBER.encode(), hashlib.sha256).hexdigest()


def picture(fmt="PNG"):
    out = BytesIO()
    Image.new("RGB", (100, 80), "white").save(out, format=fmt)
    return out.getvalue()


@override_settings(TENCENT_FACEID_SECRET_ID="fake-id", TENCENT_FACEID_SECRET_KEY="fake-key",
                   TENCENT_FACEID_TIMEOUT_SECONDS=12)
class TencentAdapterTests(SimpleTestCase):
    def setUp(self):
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network"))
        network.start()
        self.addCleanup(network.stop)

    def call(self, **changes):
        values = dict(Result="Success", Sim=99.76, Name="测试姓名", IdNum=NUMBER,
                      Warnings="", RequestId="test-request-001")
        values.update(changes)
        with patch("providers.tencent_identity.read_identity_image", return_value=picture()), \
                patch("providers.tencent_identity.FaceidClient") as client:
            client.return_value.CheckIdCardInformation.return_value = SimpleNamespace(**values)
            result = check_identity_photo(asset=object(), application_real_name="测试姓名")
            return result, client

    def test_success_uses_signed_sdk_fixed_endpoint_base64_and_no_retries(self):
        result, client = self.call()
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.similarity, Decimal("99.76"))
        self.assertEqual(result.real_name, "测试姓名")
        self.assertEqual(result.number_digest, digest())
        self.assertEqual(result.number_masked, "1101**********0015")
        self.assertNotIn("测试姓名", repr(result))
        profile = client.call_args.args[2]
        self.assertEqual(profile.httpProfile.endpoint, "faceid.tencentcloudapi.com")
        self.assertEqual(profile.httpProfile.scheme, "https")
        self.assertEqual(profile.signMethod, "TC3-HMAC-SHA256")
        self.assertEqual(type(profile.retryer).__name__, "NoopRetryer")
        request = client.return_value.CheckIdCardInformation.call_args.args[0]
        self.assertIsNone(request.ImageUrl)
        self.assertEqual(request.Config, '{"Quality":true}')
        self.assertTrue(base64.b64decode(request.ImageBase64).startswith(b"\xff\xd8"))
        self.assertNotIn(NUMBER, repr(result))
        self.assertTrue(logging.getLogger("tencentcloud_sdk_common").disabled)

    def test_similarity_boundary_and_malformed_success_fail_closed(self):
        self.assertEqual(self.call(Sim=70)[0].status, "passed")
        self.assertEqual(self.call(Sim=69.99)[0].status, "failed")
        for value in (None, "NaN", "Infinity", -1, 101, "invalid"):
            with self.subTest(value=value):
                self.assertEqual(self.call(Sim=value)[0].status, "error")
        self.assertEqual(self.call(RequestId="")[0].status, "error")
        self.assertEqual(self.call(Name=None)[0].status, "error")

    def test_ocr_must_match_application_name_and_warnings_block(self):
        self.assertEqual(self.call(Name="其他姓名")[0].code, "NameMismatch")
        self.assertEqual(self.call(IdNum="invalid-number")[0].code, "InvalidResponse")
        self.assertEqual(self.call(Name="")[0].code, "InvalidResponse")
        self.assertEqual(self.call(Warnings="-8001")[0].status, "failed")
        self.assertEqual(self.call(Result="FailedOperation.OcrFailed")[0].status, "failed")

    def test_sdk_errors_are_safe_and_do_not_retry(self):
        for code in ("UnauthorizedOperation.Arrears", "UnauthorizedOperation.Nonactivated",
                     "AuthFailure.SignatureFailure", "RequestLimitExceeded", "ClientNetworkError"):
            with self.subTest(code=code), \
                    patch("providers.tencent_identity.read_identity_image", return_value=picture()), \
                    patch("providers.tencent_identity.FaceidClient") as client:
                client.return_value.CheckIdCardInformation.side_effect = TencentCloudSDKException(
                    code, f"secret {NUMBER} base64-payload", "safe-request")
                result = check_identity_photo(asset=object(), application_real_name="测试姓名")
                self.assertEqual(result.status, "error")
                self.assertNotIn(NUMBER, repr(result))
                client.return_value.CheckIdCardInformation.assert_called_once()

    def test_legacy_webp_converts_without_metadata_and_rejects_garbage(self):
        for fmt in ("PNG", "JPEG", "WEBP"):
            encoded = encode_image(picture(fmt))
            decoded = Image.open(BytesIO(base64.b64decode(encoded)))
            self.assertEqual(decoded.format, "JPEG")
            self.assertFalse(decoded.getexif())
        with self.assertRaises(OSError):
            encode_image(b"not an image")

    def test_private_read_is_bounded_and_closed(self):
        asset = SimpleNamespace(object_key="private/synthetic.jpg")
        with patch("providers.tencent_identity.CosS3Client") as cos:
            stream = cos.return_value.get_object.return_value["Body"].get_raw_stream.return_value
            stream.read.return_value = b"bytes"
            self.assertEqual(read_identity_image(asset), b"bytes")
            stream.read.assert_called_once_with(8 * 1024 * 1024 + 1)
            stream.close.assert_called_once()
            self.assertEqual(cos.call_args.kwargs["retry"], 0)

    def test_real_sdk_serializes_signed_request_and_parses_response_offline(self):
        response = Response()
        response.status_code = 200
        response._content = json.dumps({"Response": {
            "Result": "Success", "Sim": 98, "Name": "测试姓名", "IdNum": NUMBER,
            "Warnings": "", "RequestId": "synthetic-sdk-request",
        }}).encode()
        with patch("providers.tencent_identity.read_identity_image", return_value=picture()), \
                patch("requests.sessions.Session.request", return_value=response) as network:
            result = check_identity_photo(asset=object(), application_real_name="测试姓名")
        self.assertEqual(result.status, "passed")
        network.assert_called_once()
        sent = network.call_args.kwargs
        self.assertEqual(sent["url"].rstrip("/"), "https://faceid.tencentcloudapi.com")
        self.assertTrue(sent["headers"]["Authorization"].startswith("TC3-HMAC-SHA256"))
        self.assertEqual(sent["headers"]["X-TC-Action"], "CheckIdCardInformation")
        self.assertTrue(sent["verify"])
        self.assertEqual(sent["timeout"], 12)


@override_settings(TENCENT_FACEID_ENABLED=True, TENCENT_FACEID_SECRET_ID="fake-id",
                   TENCENT_FACEID_SECRET_KEY="fake-key", TENCENT_FACEID_DAILY_LIMIT=5)
class IdentitySubmissionTests(TransactionTestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(phone="13977005001", password="synthetic-password")
        self.client.force_authenticate(self.user)
        self.profile = ProviderProfile.objects.create(
            user=self.user, status="approved", application_real_name="测试姓名",
            identity_real_name="测试姓名", identity_number_digest=digest(),
            identity_number_masked="1101**********0015",
        )
        for field in ("identity_front_photo", "identity_back_photo", "identity_face_photo"):
            setattr(self.profile, field, MediaAsset.objects.create(
                owner=self.user, scope="private", category="identity", status="uploaded",
                object_key=f"private/{field}.jpg", content_type="image/jpeg", etag=field,
            ))
        self.profile.save()
        self.result = CheckResult("passed", "Success", "身份证验真通过，待人工审核。", "request-001", Decimal("90"),
                                  "测试姓名", digest(), "1101**********0015")
        for target, kwargs in (
            ("requests.sessions.Session.request", {"side_effect": AssertionError("No network")}),
            ("providers.serializers.build_media_url", {"return_value": "https://private.invalid/signed"}),
        ):
            mocker = patch(target, **kwargs)
            mocker.start()
            self.addCleanup(mocker.stop)

    def submit(self, consent=CONSENT_VERSION):
        return submit_provider_identity(provider=self.profile, consent_version=consent)

    def receipt(self, status="processing", age=0, **kwargs):
        check = ProviderIdentityCheck.objects.create(
            provider=self.profile, fingerprint=fingerprint(self.profile), consent_version=CONSENT_VERSION,
            status=status, **kwargs,
        )
        if age:
            ProviderIdentityCheck.objects.filter(pk=check.pk).update(created_at=timezone.now() - timedelta(seconds=age))
            check.refresh_from_db()
        return check

    def test_consent_and_name_checks_happen_before_channel(self):
        with patch("providers.identity_verification.check_identity_photo") as channel:
            for consent in ("", "wrong-version"):
                with self.assertRaises(ValidationError):
                    self.submit(consent)
            self.profile.application_real_name = ""
            self.profile.save()
            with self.assertRaises(ValidationError):
                self.submit()
            channel.assert_not_called()
        self.assertFalse(ProviderIdentityCheck.objects.exists())

    def test_success_is_only_pending_and_receipt_is_minimal(self):
        def verify(**kwargs):
            self.assertFalse(connection.in_atomic_block, "Network must run outside DB locks")
            self.assertEqual(kwargs["asset"], self.profile.identity_front_photo)
            self.assertEqual(set(kwargs), {"asset", "application_real_name"})
            self.assertEqual(kwargs["application_real_name"], "测试姓名")
            return self.result
        with patch("providers.identity_verification.check_identity_photo", side_effect=verify) as channel:
            response = self.client.post("/api/v1/providers/me/identity/submit/",
                                        {"consent_version": CONSENT_VERSION}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["data"]["identity_status"], "pending")
            self.assertEqual(response.data["data"]["identity_verification"]["status"], "passed")
            self.assertNotIn("request_id", response.data["data"]["identity_verification"])
            self.assertIn("no-store", response["Cache-Control"])
            with self.assertRaises(ValidationError):
                self.submit()
            channel.assert_called_once()
        check = ProviderIdentityCheck.objects.get()
        self.assertEqual(check.consent_version, CONSENT_VERSION)
        self.assertEqual(check.request_id, "request-001")
        self.assertNotIn(NUMBER, repr(check.__dict__))
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.identity_verification_required)
        require_verified_check(self.profile)

    def test_photos_only_save_and_submit_auto_fill_identity_from_successful_channel(self):
        saved = self.client.patch("/api/v1/providers/me/identity/", {
            "recognition_mode": "photo",
            **{f"{kind}_id": str(getattr(self.profile, f"{kind}_id")) for kind in (
                "identity_front_photo", "identity_back_photo", "identity_face_photo")},
        }, format="json")
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data["data"]["identity_real_name"], "")
        self.assertEqual(saved.data["data"]["identity_number_masked"], "")
        with patch("providers.identity_verification.check_identity_photo", return_value=self.result):
            response = self.client.post("/api/v1/providers/me/identity/submit/", {
                "recognition_mode": "photo", "consent_version": CONSENT_VERSION,
            }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["identity_real_name"], "测试姓名")
        self.assertEqual(response.data["data"]["identity_number_masked"], "1101**********0015")
        self.assertEqual(response.data["data"]["identity_verification"]["status"], "passed")
        self.assertNotIn(NUMBER, json.dumps(response.data, default=str))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.identity_number_digest, digest())
        require_verified_check(self.profile)

    def test_selfie_remains_required_and_is_not_sent_to_channel(self):
        self.profile.identity_face_photo = None
        self.profile.save()
        with patch("providers.identity_verification.check_identity_photo") as channel:
            with self.assertRaisesMessage(ValidationError, "本人核验照片"):
                self.submit()
            channel.assert_not_called()

    def test_photo_mode_rejects_client_supplied_name_and_id(self):
        for value in ({"identity_real_name": "伪造姓名"}, {"id_number": NUMBER}):
            response = self.client.patch("/api/v1/providers/me/identity/",
                                         {"recognition_mode": "photo", **value}, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertIn("自动识别", str(response.data))

    def test_changing_photo_clears_recognized_data_and_invalidates_receipt(self):
        with patch("providers.identity_verification.check_identity_photo", return_value=self.result):
            self.submit()
        ProviderProfile.objects.filter(pk=self.profile.pk).update(identity_status="rejected")
        changed = MediaAsset.objects.create(owner=self.user, scope="private", category="identity",
                                           status="uploaded", object_key="private/new-selfie.jpg")
        profile = save_provider_identity(provider=self.profile, data={
            "recognition_mode": "photo", "identity_face_photo": changed,
        })
        self.assertFalse(profile.identity_real_name)
        self.assertFalse(profile.identity_number_masked)
        self.assertFalse(profile.identity_number_digest)
        self.assertEqual(verification_summary(profile)["status"], "not_checked")
        with self.assertRaises(ValidationError):
            require_verified_check(profile)

    def test_incomplete_successful_result_never_writes_identity_or_enters_review(self):
        profile = save_provider_identity(provider=self.profile, data={"recognition_mode": "photo"})
        with patch("providers.identity_verification.check_identity_photo",
                   return_value=CheckResult("passed", "Success", "unexpected success")):
            with self.assertRaises(IdentityVerificationUnavailable):
                self.submit()
        profile.refresh_from_db()
        self.assertFalse(profile.identity_real_name)
        self.assertEqual(profile.identity_status, "unverified")
        self.assertEqual(ProviderIdentityCheck.objects.get().status, "error")

    @override_settings(TENCENT_FACEID_ENABLED=False)
    def test_photo_only_client_can_save_but_never_falls_back_to_manual_submission(self):
        profile = save_provider_identity(provider=self.profile, data={"recognition_mode": "photo"})
        self.assertTrue(profile.identity_verification_required)
        with patch("providers.identity_verification.check_identity_photo") as channel:
            with self.assertRaises(IdentityVerificationUnavailable):
                self.submit()
            channel.assert_not_called()
        profile.refresh_from_db()
        self.assertEqual(profile.identity_status, "unverified")

    def test_failure_persists_and_never_enters_review_or_auto_passes(self):
        with patch("providers.identity_verification.check_identity_photo", return_value=CheckResult(
            "failed", "NameMismatch", "姓名不一致", "request-002",
        )) as channel:
            with self.assertRaisesMessage(ValidationError, "姓名不一致"):
                self.submit()
            self.profile.refresh_from_db()
            self.assertEqual(self.profile.identity_status, "unverified")
            self.assertEqual(ProviderIdentityCheck.objects.get().status, "failed")
            with self.assertRaisesMessage(ValidationError, "30秒"):
                self.submit()
            channel.assert_called_once()
            with self.assertRaises(ValidationError):
                require_verified_check(self.profile)

    def test_channel_failure_and_unexpected_error_fail_closed(self):
        with patch("providers.identity_verification.check_identity_photo", side_effect=RuntimeError(NUMBER)):
            with self.assertRaises(IdentityVerificationUnavailable) as error:
                self.submit()
        self.assertNotIn(NUMBER, str(error.exception))
        self.assertEqual(ProviderIdentityCheck.objects.get().status, "error")
        with override_settings(TENCENT_FACEID_ENABLED=False):
            with self.assertRaises(IdentityVerificationUnavailable):
                self.submit()

    def test_processing_blocks_concurrent_submit_and_save(self):
        self.receipt()
        with patch("providers.identity_verification.check_identity_photo") as channel:
            with self.assertRaisesMessage(ValidationError, "正在验真"):
                self.submit()
            with self.assertRaises(ValidationError):
                save_provider_identity(provider=self.profile, data={"identity_real_name": "新名字"})
            channel.assert_not_called()

    def test_reentrant_submission_while_remote_request_is_inflight_does_not_call_twice(self):
        def verify(**kwargs):
            with self.assertRaisesMessage(ValidationError, "正在验真"):
                self.submit()
            return self.result
        with patch("providers.identity_verification.check_identity_photo", side_effect=verify) as channel:
            self.submit()
            channel.assert_called_once()
        self.assertEqual(ProviderIdentityCheck.objects.count(), 1)

    def test_expired_attempt_can_be_retried_but_old_result_cannot_override(self):
        old = self.receipt(age=121)
        with patch("providers.identity_verification.check_identity_photo", return_value=self.result):
            self.assertEqual(self.submit().identity_status, "pending")
        old.refresh_from_db()
        self.assertEqual(old.result_code, "ExpiredAttempt")
        self.assertEqual(self.profile.identity_checks.first().status, "passed")

    def test_material_or_account_change_during_call_discards_late_result(self):
        def change(**kwargs):
            ProviderProfile.objects.filter(pk=self.profile.pk).update(identity_real_name="已被修改")
            return self.result
        with patch("providers.identity_verification.check_identity_photo", side_effect=change):
            with self.assertRaises(IdentityVerificationUnavailable):
                self.submit()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.identity_status, "unverified")
        self.assertEqual(ProviderIdentityCheck.objects.get().result_code, "StaleAttempt")

    def test_superseded_response_cannot_override_newer_passed_attempt(self):
        def verify(**kwargs):
            self.receipt(status="passed", completed_at=timezone.now(), request_id="newer-request")
            return self.result
        with patch("providers.identity_verification.check_identity_photo", side_effect=verify):
            with self.assertRaises(IdentityVerificationUnavailable):
                self.submit()
        latest = self.profile.identity_checks.first()
        self.assertEqual(latest.status, "passed")
        self.assertEqual(latest.request_id, "newer-request")

    def test_successful_unchanged_materials_reuse_receipt_for_24_hours(self):
        with patch("providers.identity_verification.check_identity_photo", return_value=self.result) as channel:
            self.submit()
            ProviderProfile.objects.filter(pk=self.profile.pk).update(identity_status="rejected")
            self.assertEqual(self.submit().identity_status, "pending")
            channel.assert_called_once()

    def test_both_admin_review_routes_require_passed_receipt(self):
        from django.urls import reverse
        admin = User.objects.create_superuser(phone="13977005002", password="synthetic-password")
        self.client.force_authenticate(admin)
        self.profile.identity_verification_required = True
        self.profile.identity_status = "pending"
        self.profile.onboarding_status = "approved"
        self.profile.save()
        route = reverse("backoffice-provider-identity-review", args=(self.profile.pk,))
        response = self.client.post(route, {"decision": "approve"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("验真", str(response.data))

        self.profile.onboarding_status = "pending_review"
        self.profile.save()
        ProviderProfileRevision.objects.create(
            provider=self.profile, display_name="合成测试", bio="用于离线测试的资料。",
            lifestyle_photo=self.profile.identity_front_photo,
            service_city_code="130400", service_city_name="邯郸市",
        )
        category = ServiceCategory.objects.create(name="合成分类", slug="synthetic-identity")
        ProviderServiceRevision.objects.create(
            provider=self.profile, category=category, action="create", billing_type="hourly",
            price_amount=10000,
        )
        route = reverse("backoffice-provider-change-review-action", args=("onboarding", self.profile.pk))
        response = self.client.post(route, {"decision": "approve"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("验真", str(response.data))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.identity_status, "pending")

    def test_admin_receipt_is_permission_gated_and_provider_read_does_not_call_channel(self):
        from backoffice.serializers import ProviderAdminSerializer
        self.receipt(status="passed", request_id="safe-request", similarity=Decimal("99"))
        with patch("providers.identity_verification.check_identity_photo") as channel:
            response = self.client.get("/api/v1/providers/me/identity/")
            self.assertEqual(response.status_code, 200)
            channel.assert_not_called()
        serializer = ProviderAdminSerializer(context={"can_review": False, "include_detail": True})
        self.assertIsNone(serializer.get_identity_verification(self.profile))
        serializer = ProviderAdminSerializer(context={"can_review": True, "include_detail": True})
        summary = serializer.get_identity_verification(self.profile)
        self.assertEqual(summary["request_id"], "safe-request")

    def test_daily_limit_missing_configuration_and_private_asset_ownership(self):
        for _ in range(5):
            self.receipt(status="failed", age=60)
        with patch("providers.identity_verification.check_identity_photo") as channel:
            with self.assertRaisesMessage(ValidationError, "24小时"):
                self.submit()
            with override_settings(TENCENT_FACEID_SECRET_KEY=""):
                with self.assertRaises(IdentityVerificationUnavailable):
                    self.submit()
            self.profile.identity_front_photo.scope = "public"
            self.profile.identity_front_photo.save()
            with self.assertRaisesMessage(ValidationError, "无权"):
                self.submit()
            channel.assert_not_called()

    def test_review_cannot_use_stale_receipt_after_material_edit(self):
        with patch("providers.identity_verification.check_identity_photo", return_value=self.result):
            self.submit()
        self.profile.refresh_from_db()
        self.profile.identity_number_digest = "changed"
        self.profile.save()
        with self.assertRaises(ValidationError):
            require_verified_check(self.profile)
        self.assertEqual(verification_summary(self.profile)["status"], "not_checked")

    @override_settings(TENCENT_FACEID_ENABLED=False)
    def test_disabled_keeps_legacy_manual_flow_without_claiming_machine_verification(self):
        with patch("providers.identity_verification.check_identity_photo") as channel:
            profile = self.submit("")
            self.assertEqual(profile.identity_status, "pending")
            require_verified_check(profile)
            self.assertEqual(verification_summary(profile)["status"], "not_checked")
            channel.assert_not_called()
