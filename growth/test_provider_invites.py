import io
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from accounts.models import User
from backoffice.models import (
    AdminAuditLog,
    AdminRole,
    Organization,
    OrganizationMember,
    PlatformOperationSetting,
)
from mediafiles.models import MediaAsset
from providers.models import ProviderProfile
from providers.services import save_provider_application
from rest_framework.exceptions import ValidationError
from .models import Invitation, ProviderInviteConfig, ProviderInviteReward, ProviderInviteSource


REGISTER = "/api/v1/growth/provider-invites/register/"
ADMIN = "/api/v1/admin/provider-invites/"
MY = "/api/v1/growth/provider-invites/me/"


def photo(content_type="image/jpeg", filename="phone-photo.jpg"):
    stream = io.BytesIO()
    Image.new("RGB", (32, 48), "teal").save(stream, format="JPEG")
    return SimpleUploadedFile(filename, stream.getvalue(), content_type=content_type)


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    PROVIDER_INVITE_H5_BASE_URL="https://provider.example.test",
)
class ProviderInviteTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.sms = patch("accounts.serializers.verify_sms_code").start()
        self.upload = patch("mediafiles.views.upload_public_stream", return_value="etag").start()
        self.cleanup = patch("growth.provider_invite_views.delete_public_object").start()
        self.addCleanup(patch.stopall)
        self.config = ProviderInviteConfig.objects.create(
            enabled=True, store_reward_amount=5000, provider_reward_amount=3000, revision=1
        )
        self.source = ProviderInviteSource.objects.create(kind="store", name="测试合作门店")
        self.admin = User.objects.create_superuser(phone="13800009001", password="test-password")

    def register(self, **values):
        payload = {
            "phone": "13800009002",
            "code": "123456",
            "password": "test-password",
            "source_code": self.source.code,
            "application_real_name": "测试新人",
            "application_birth_date": "2000-01-01",
            "gender": "female",
            "service_city_code": "130400",
            "agreement_accepted": "true",
            "file": photo(),
        }
        payload.update(values)
        return self.client.post(REGISTER, payload, format="multipart")

    def make_reward(self):
        response = self.register()
        self.assertEqual(response.status_code, 201, response.data)
        return ProviderInviteReward.objects.get()

    def authorize(self, permissions, *, all_data=True):
        org = Organization.objects.create(
            name="测试运营", code="test", organization_type="platform" if all_data else "city_agent"
        )
        role = AdminRole.objects.create(
            organization=org,
            name="测试岗位",
            code="role",
            data_scope="all",
            permissions=permissions,
        )
        staff = User.objects.create_user(phone="13800009003", password="test-password")
        OrganizationMember.objects.create(user=staff, organization=org, role=role)
        self.client.force_authenticate(staff)
        return role

    def act(self, reward, action="approve", **extras):
        return self.client.post(
            f"{ADMIN}rewards/{reward.public_id}/action/",
            {
                "action": action,
                "revision": reward.revision,
                "note": "核验真实邀请及收款人",
                "confirmed": True,
                **extras,
            },
            format="json",
        )

    def test_registration_creates_account_intent_and_one_cash_reward_not_coupon(self):
        result = self.register(invite_code=str(self.admin.public_id), amount="999999")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertIn("access", result.data["data"])
        reward = ProviderInviteReward.objects.select_related("profile", "invitee").get()
        self.assertEqual(reward.amount, 5000)
        self.assertEqual(reward.status, "pending")
        self.assertEqual(reward.profile.status, "pending")
        self.assertEqual(reward.profile.invitation_code, self.source.code)
        self.assertEqual(reward.invitee.gender, "female")
        self.assertEqual(reward.profile.service_city_name, "邯郸市")
        self.assertEqual(reward.profile.lifestyle_photo.owner_id, reward.invitee_id)
        self.assertFalse(Invitation.objects.exists())
        self.sms.assert_called_once_with(phone="13800009002", purpose="register", code="123456")

    def test_repeat_phone_never_rebinds_or_rewards_again(self):
        self.make_reward()
        source = ProviderInviteSource.objects.create(kind="store", name="另一门店")
        cache.clear()
        result = self.register(source_code=source.code)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(ProviderInviteReward.objects.count(), 1)
        self.assertEqual(ProviderInviteReward.objects.get().source_id, self.source.pk)

    def test_existing_user_cannot_be_rewarded(self):
        result = self.register(phone=self.admin.phone)
        self.assertEqual(result.status_code, 400)
        self.assertFalse(ProviderInviteReward.objects.exists())

    def test_unknown_and_disabled_sources_are_rejected_without_upload(self):
        self.assertEqual(self.register(source_code="ABCDEF123456").status_code, 404)
        self.source.active = False
        self.source.save()
        self.assertEqual(self.register().status_code, 400)
        self.assertEqual(
            self.client.get(f"/api/v1/growth/provider-invites/{self.source.code}/").status_code, 400
        )
        self.upload.assert_not_called()

    def test_disabled_campaign_blocks_registration(self):
        self.config.enabled = False
        self.config.save()
        self.assertEqual(self.register().status_code, 400)
        self.assertFalse(ProviderInviteReward.objects.exists())

    def test_validation_requires_consent_adult_gender_city_and_photo(self):
        for value in [
            {"agreement_accepted": "false"},
            {"application_birth_date": "2020-01-01"},
            {"gender": "unspecified"},
            {"service_city_code": "999999"},
            {"application_real_name": "测"},
        ]:
            cache.clear()
            self.assertEqual(self.register(**value).status_code, 400, value)
        self.upload.assert_not_called()
        self.assertFalse(ProviderProfile.objects.exists())

    def test_invalid_image_rolls_back_account_and_reward(self):
        result = self.register(
            file=SimpleUploadedFile("fake.jpg", b"not-image", content_type="image/jpeg")
        )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(User.objects.filter(phone="13800009002").exists())
        self.assertFalse(ProviderInviteReward.objects.exists())

    def test_mobile_mime_mismatch_uses_verified_image_bytes(self):
        self.assertEqual(
            self.register(
                file=photo(content_type="application/octet-stream", filename="apple-photo.png")
            ).status_code,
            201,
        )
        self.assertEqual(
            MediaAsset.objects.get(category="provider_photo").content_type, "image/jpeg"
        )

    def test_upload_failure_rolls_back_everything(self):
        self.upload.side_effect = OSError("synthetic storage failure")
        result = self.register()
        self.assertEqual(result.status_code, 400)
        self.assertFalse(User.objects.filter(phone="13800009002").exists())
        self.assertFalse(MediaAsset.objects.exists())
        self.assertFalse(ProviderProfile.objects.exists())

    def test_failure_after_upload_removes_uploaded_object(self):
        with patch(
            "growth.provider_invite_views.submit_provider_application",
            side_effect=ValidationError("模拟提交失败"),
        ):
            self.assertEqual(self.register().status_code, 400)
        self.cleanup.assert_called_once()
        self.assertFalse(User.objects.filter(phone="13800009002").exists())
        self.assertFalse(ProviderInviteReward.objects.exists())

    def test_snapshot_unchanged_after_config_or_source_change(self):
        reward = self.make_reward()
        self.config.store_reward_amount = 9900
        self.config.save()
        self.source.name = "门店改名"
        self.source.save()
        reward.refresh_from_db()
        self.assertEqual(reward.amount, 5000)
        self.assertEqual(reward.source_name_snapshot, "测试合作门店")
        self.assertEqual(reward.config_revision, 1)

    def test_provider_inviter_eligibility_own_summary_and_separate_amount(self):
        inviter = User.objects.create_user(phone="13800009004", password="test-password")
        self.client.force_authenticate(inviter)
        self.assertEqual(self.client.post(MY).status_code, 404)
        ProviderProfile.objects.create(user=inviter, status="approved", display_name="测试达人")
        code = self.client.post(MY).data["data"]["code"]
        self.assertEqual(self.client.post(MY).data["data"]["code"], code)
        self.client.force_authenticate(None)
        self.assertEqual(self.register(source_code=code).status_code, 201)
        self.assertEqual(ProviderInviteReward.objects.get().amount, 3000)
        self.client.force_authenticate(inviter)
        result = self.client.get(MY).data["data"]
        self.assertTrue(result["enabled"])
        self.assertTrue(result["qr_data"].startswith("data:image/png;base64,"))
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn("real_name", result["items"][0])
        self.assertNotIn("transfer_reference", result["items"][0])

    def test_suspended_inviter_blocks_new_registration(self):
        inviter = User.objects.create_user(phone="13800009004", password="test-password")
        ProviderProfile.objects.create(user=inviter, status="suspended")
        source = ProviderInviteSource.objects.create(
            kind="provider", name="测试达人", provider_user=inviter
        )
        self.assertEqual(self.register(source_code=source.code).status_code, 400)

    def test_public_response_has_only_public_source_and_configured_cities(self):
        PlatformOperationSetting.objects.create(
            discovery_cities=[{"city_code": "110100", "city_name": "北京市"}]
        )
        response = self.client.get(f"/api/v1/growth/provider-invites/{self.source.code}/")
        self.assertEqual(set(response.data["data"]), {"code", "name", "kind", "cities"})
        self.assertEqual(response.data["data"]["cities"], [{"code": "110100", "name": "北京市"}])
        self.assertEqual(self.register().status_code, 400)

    def test_finance_lifecycle_and_duplicate_payment_rejected(self):
        reward = self.make_reward()
        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.act(
                reward, "paid", transfer_reference="test-001", paid_at=timezone.now().isoformat()
            ).status_code,
            400,
        )
        self.assertEqual(self.act(reward).status_code, 200)
        reward.refresh_from_db()
        self.assertEqual(
            self.act(
                reward, "paid", transfer_reference="test-001", paid_at=timezone.now().isoformat()
            ).status_code,
            200,
        )
        self.assertEqual(
            self.act(
                reward, "paid", transfer_reference="test-001", paid_at=timezone.now().isoformat()
            ).status_code,
            400,
        )
        reward.refresh_from_db()
        self.assertEqual(reward.status, "paid")
        self.assertEqual(
            AdminAuditLog.objects.filter(action__startswith="provider_invite.").count(), 2
        )

    def test_payout_requires_receipt_time_confirmation_and_no_future_or_before_review(self):
        reward = self.make_reward()
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.act(reward).status_code, 200)
        reward.refresh_from_db()
        for extras in [
            {},
            {"confirmed": False},
            {
                "paid_at": (timezone.now() + timedelta(hours=1)).isoformat(),
                "transfer_reference": "r",
            },
            {
                "paid_at": (timezone.now() - timedelta(days=1)).isoformat(),
                "transfer_reference": "r",
            },
        ]:
            self.assertEqual(self.act(reward, "paid", **extras).status_code, 400)
        reward.refresh_from_db()
        self.assertEqual(reward.status, "approved")

    def test_permissions_separate_management_review_and_payout(self):
        reward = self.make_reward()
        role = self.authorize(["provider_invite.view", "provider_invite.manage"])
        self.assertEqual(self.client.get(ADMIN + "rewards/").status_code, 200)
        self.assertEqual(self.act(reward).status_code, 403)
        role.permissions += ["provider_invite.review"]
        role.save()
        self.assertEqual(self.act(reward).status_code, 200)
        reward.refresh_from_db()
        self.assertEqual(
            self.act(
                reward, "paid", transfer_reference="r", paid_at=timezone.now().isoformat()
            ).status_code,
            403,
        )

    def test_regional_wildcard_cannot_read_platform_rewards(self):
        self.authorize(["*"], all_data=False)
        self.assertEqual(self.client.get(ADMIN + "rewards/").status_code, 403)
        self.assertEqual(self.client.get(ADMIN + "sources/").status_code, 403)

    def test_config_revision_and_zero_reward_guard(self):
        self.client.force_authenticate(self.admin)
        payload = {
            "enabled": True,
            "store_reward_amount": 0,
            "provider_reward_amount": 100,
            "revision": 1,
        }
        self.assertEqual(self.client.put(ADMIN + "config/", payload).status_code, 400)
        payload["store_reward_amount"] = 100
        self.assertEqual(self.client.put(ADMIN + "config/", payload).status_code, 200)
        self.assertEqual(self.client.put(ADMIN + "config/", payload).status_code, 400)

    def test_rejected_reward_cannot_be_paid_and_audit_failure_rolls_back(self):
        reward = self.make_reward()
        self.client.force_authenticate(self.admin)
        with patch(
            "backoffice.provider_invite_views.audit", side_effect=ValidationError("模拟审计失败")
        ):
            self.assertEqual(self.act(reward).status_code, 400)
        reward.refresh_from_db()
        self.assertEqual(reward.status, "pending")
        self.assertEqual(self.act(reward, "reject").status_code, 200)
        reward.refresh_from_db()
        self.assertEqual(
            self.act(
                reward, "paid", transfer_reference="r", paid_at=timezone.now().isoformat()
            ).status_code,
            400,
        )

    def test_bound_invitation_cannot_be_changed_after_rejection(self):
        reward = self.make_reward()
        reward.profile.status = "rejected"
        reward.profile.save()
        with self.assertRaises(ValidationError):
            save_provider_application(user=reward.invitee, data={"invitation_code": "another"})

    def test_database_enforces_one_reward_per_invitee(self):
        reward = self.make_reward()
        reward.pk = None
        reward.public_id = uuid.uuid4()
        with self.assertRaises(IntegrityError), transaction.atomic():
            reward.save(force_insert=True)

    def test_admin_source_pagination_qr_and_optimistic_update(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(ADMIN + "sources/", {"name": "新增测试门店"})
        self.assertEqual(response.status_code, 201)
        data = response.data["data"]
        self.assertEqual(
            self.client.get(ADMIN + "sources/?page_size=1").data["data"]["pagination"]["total"], 2
        )
        url = ADMIN + f"sources/{data['public_id']}/"
        self.assertTrue(
            self.client.get(url).data["data"]["qr_data"].startswith("data:image/png;base64,")
        )
        self.assertEqual(self.client.patch(url, {**data, "active": False}).status_code, 200)
        self.assertEqual(self.client.patch(url, data).status_code, 400)

    def test_invalid_sms_does_not_create_user_or_upload(self):
        self.sms.side_effect = ValidationError({"code": "验证码错误或已过期。"})
        self.assertEqual(self.register().status_code, 400)
        self.upload.assert_not_called()
        self.assertFalse(User.objects.filter(phone="13800009002").exists())

    def test_source_disabled_during_upload_rolls_back(self):
        def disable_source(**kwargs):
            ProviderInviteSource.objects.filter(pk=self.source.pk).update(active=False)
            return "etag"

        self.upload.side_effect = disable_source
        self.assertEqual(self.register().status_code, 400)
        self.cleanup.assert_called_once()
        self.assertFalse(ProviderInviteReward.objects.exists())

    def test_duplicate_receipt_cannot_pay_another_reward(self):
        first = self.make_reward()
        self.assertEqual(self.register(phone="13800009005").status_code, 201)
        second = ProviderInviteReward.objects.exclude(pk=first.pk).get()
        self.client.force_authenticate(self.admin)
        for reward in (first, second):
            self.assertEqual(self.act(reward).status_code, 200)
            reward.refresh_from_db()
        self.assertEqual(
            self.act(
                first,
                "paid",
                transfer_reference="unique-bank-01",
                paid_at=timezone.now().isoformat(),
            ).status_code,
            200,
        )
        self.assertEqual(
            self.act(
                second,
                "paid",
                transfer_reference="unique-bank-01",
                paid_at=timezone.now().isoformat(),
            ).status_code,
            400,
        )
        second.refresh_from_db()
        self.assertEqual(second.status, "approved")

    def test_unpaid_rewards_block_inviter_closure_and_summary_stays_private(self):
        from accounts.account_closure import account_closure_blockers

        inviter = User.objects.create_user(phone="13800009004", password="test-password")
        profile = ProviderProfile.objects.create(user=inviter, status="approved")
        source = ProviderInviteSource.objects.create(
            kind="provider", name="测试邀请达人", provider_user=inviter
        )
        self.assertEqual(self.register(source_code=source.code).status_code, 201)
        self.assertIn(
            "provider_invite_rewards", {r["code"] for r in account_closure_blockers(inviter)}
        )
        profile.status = "suspended"
        profile.save()
        self.client.force_authenticate(inviter)
        result = self.client.get(MY).data["data"]
        self.assertFalse(result["enabled"])
        self.assertEqual(result["pagination"]["total"], 1)
        self.client.force_authenticate(self.admin)
        ProviderProfile.objects.create(user=self.admin, status="approved")
        result = self.client.get(MY).data["data"]
        self.assertEqual(result["pagination"]["total"], 0)

    def test_summary_aggregates_by_status_not_creation_time(self):
        self.make_reward()
        self.assertEqual(self.register(phone="13800009005").status_code, 201)
        self.client.force_authenticate(self.admin)
        result = self.client.get(ADMIN + "rewards/").data["data"]
        self.assertEqual(result["summary"], [{"status": "pending", "count": 2, "amount": 10000}])

    def test_qr_uses_explicit_provider_h5_base_and_ignores_request_host(self):
        self.client.force_authenticate(self.admin)
        with override_settings(PROVIDER_INVITE_H5_BASE_URL="https://provider.example.test/work/", ALLOWED_HOSTS=["evil.example.test"]):
            response = self.client.get(
                ADMIN + f"sources/{self.source.public_id}/", HTTP_HOST="evil.example.test"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["invite_url"],
                         f"https://provider.example.test/work/#/pages/invitations/provider-register?code={self.source.code}")

    def test_missing_or_invalid_h5_blocks_new_invites_not_existing_ledger(self):
        reward = self.make_reward()
        self.client.force_authenticate(self.admin)
        for base in ("", "http://provider.example.test", "https://provider.example.test/#/", "https://provider.example.test/?x=1", "https://user:secret@provider.example.test"):
            with self.subTest(base=base), override_settings(PROVIDER_INVITE_H5_BASE_URL=base):
                self.assertEqual(self.client.get(ADMIN + f"sources/{self.source.public_id}/").status_code, 400)
                self.assertEqual(self.client.put(ADMIN + "config/", {
                    "enabled": True, "store_reward_amount": 5000, "provider_reward_amount": 3000, "revision": 1,
                }).status_code, 400)
                response = self.client.get(ADMIN + "rewards/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"]["items"][0]["source"]["invite_url"], "")
                self.assertEqual(self.register(phone="13800009008").status_code, 400)
        with override_settings(PROVIDER_INVITE_H5_BASE_URL=""):
            self.assertEqual(self.act(reward).status_code, 200)

    def test_registered_account_can_read_progress_but_not_use_workbench(self):
        registered = self.register()
        self.assertEqual(registered.status_code, 201)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + registered.data["data"]["access"])
        progress = self.client.get("/api/v1/providers/me/application/")
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.data["data"]["status"], "pending")
        self.assertEqual(progress.data["data"]["invitation_code"], self.source.code)
        self.assertEqual(self.client.get("/api/v1/providers/me/workbench/").status_code, 403)
        self.client.credentials()
        login = self.client.post("/api/v1/auth/login/password/", {
            "phone": "13800009002", "password": "test-password",
        })
        self.assertEqual(login.status_code, 200)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + login.data["data"]["access"])
        self.assertEqual(self.client.get("/api/v1/providers/me/application/").data["data"]["status"], "pending")
        self.assertEqual(ProviderInviteReward.objects.count(), 1)
