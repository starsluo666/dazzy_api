import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from notifications.models import UserNotification
from orders.commission import commission_snapshot, provider_commission_overview
from orders.coupon_batches import process_coupon_batch
from orders.coupons import issue_coupon
from orders.models import CouponIssueBatch, CouponTemplate, UserCoupon
from providers.models import ProviderCategoryGrant, ProviderProfile, ServiceCategory
from .models import AdminAuditLog, AdminRole, Organization, OrganizationMember


class CouponBatchTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(phone="13977110000", password="test-password")
        self.user = User.objects.create_user(phone="13977110001")
        self.provider_user = User.objects.create_user(phone="13977110002")
        ProviderProfile.objects.create(user=self.provider_user)
        self.template = CouponTemplate.objects.create(name="测试券", face_amount=2000, min_order_amount=10000, valid_days=30)
        self.client.force_authenticate(self.admin)

    def preview(self, key=None):
        response = self.client.post("/api/v1/admin/coupon-batches/", {
            "template_public_id": str(self.template.public_id), "request_id": str(key or uuid.uuid4()),
        }, format="json")
        self.assertIn(response.status_code, (200, 201), response.data)
        return CouponIssueBatch.objects.get(public_id=response.data["data"]["public_id"])

    def action(self, batch, action="confirm"):
        return self.client.post(f"/api/v1/admin/coupon-batches/{batch.public_id}/", {
            "action": action, "confirmed": True,
        }, format="json")

    def test_preview_is_snapshot_and_never_issues_without_confirmation(self):
        for i, state in enumerate(("restricted", "suspended", "closure_pending", "closed")):
            User.objects.create_user(phone=f"1397711001{i}", account_status=state)
        User.objects.create_user(phone="13977110020", is_staff=True)
        User.objects.create_user(phone="13977110021", is_active=False)
        key = uuid.uuid4()
        batch = self.preview(key)
        self.assertEqual(batch.recipients.count(), 2)
        self.assertEqual(self.preview(key).pk, batch.pk)
        process_coupon_batch(batch.pk)
        self.assertEqual(UserCoupon.objects.count(), 0)
        User.objects.create_user(phone="13977110022")
        self.assertEqual(self.action(batch).status_code, 200)
        self.assertEqual(self.action(batch).status_code, 200)
        process_coupon_batch(batch.pk, chunk_size=1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "running")
        process_coupon_batch(batch.pk)
        process_coupon_batch(batch.pk)
        self.assertEqual(UserCoupon.objects.count(), 2)
        self.assertEqual(UserNotification.objects.filter(event_type="coupon_issued").count(), 2)
        self.assertEqual(AdminAuditLog.objects.filter(action="coupon.batch.confirm").count(), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "completed")

    def test_expired_or_changed_preview_requires_fresh_confirmation(self):
        batch = self.preview()
        self.template.face_amount = 3000
        self.template.save()
        self.assertEqual(self.action(batch).status_code, 400)
        batch = self.preview()
        CouponIssueBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.action(batch).status_code, 400)
        self.assertEqual(UserCoupon.objects.count(), 0)

    def test_committed_batch_freezes_rules_and_rechecks_recipient_status(self):
        batch = self.preview()
        self.assertEqual(self.action(batch).status_code, 200)
        self.template.face_amount, self.template.valid_days, self.template.is_active = 9000, 1, False
        self.template.save()
        self.user.account_status = "closure_pending"
        self.user.save()
        process_coupon_batch(batch.pk)
        coupon = UserCoupon.objects.get()
        self.assertEqual(coupon.owner_id, self.provider_user.pk)
        self.assertEqual(coupon.face_amount, 2000)
        self.assertGreater(coupon.expires_at, timezone.now() + timedelta(days=29))
        self.assertEqual(batch.recipients.filter(status="skipped").count(), 1)

    def test_failure_rolls_back_coupon_and_notification_then_retries_only_failed(self):
        batch = self.preview()
        self.action(batch)

        def fail_after_create(**kwargs):
            coupon = issue_coupon(**kwargs)
            if kwargs["owner"].pk == self.user.pk:
                raise RuntimeError("simulated failure after issuing")
            return coupon

        with patch("orders.coupon_batches.issue_coupon", side_effect=fail_after_create):
            process_coupon_batch(batch.pk)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "partial")
        self.assertEqual(UserCoupon.objects.count(), 1)
        self.assertEqual(UserNotification.objects.filter(recipient=self.user).count(), 0)
        self.assertEqual(self.action(batch, "retry").status_code, 200)
        self.assertEqual(self.action(batch, "retry").status_code, 200)
        process_coupon_batch(batch.pk)
        self.assertEqual(UserCoupon.objects.count(), 2)
        self.assertEqual(AdminAuditLog.objects.filter(action="coupon.batch.retry").count(), 1)

    def test_scope_and_explicit_confirmation(self):
        batch = self.preview()
        url = f"/api/v1/admin/coupon-batches/{batch.public_id}/"
        self.assertEqual(self.client.post(url, {"action": "confirm", "confirmed": False}, format="json").status_code, 400)
        org = Organization.objects.create(name="测试城市", code="coupon-city", organization_type="city_agent", city_codes=["130400"])
        role = AdminRole.objects.create(organization=org, name="运营", code="coupon-ops", permissions=["coupon.view", "coupon.issue"], data_scope="all")
        OrganizationMember.objects.create(user=self.user, organization=org, role=role)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/admin/coupon-batches/").status_code, 403)
        self.assertEqual(self.action(batch).status_code, 403)
        self.assertEqual(self.client.post("/api/v1/admin/coupon-batches/", {}, format="json").status_code, 403)
        self.assertFalse(self.client.get("/api/v1/admin/coupon-templates/").data["data"]["can_issue_all"])

    def test_single_user_issue_idempotency_and_no_cross_user_reuse(self):
        payload = {"user_public_id": str(self.user.public_id), "template_public_id": str(self.template.public_id), "request_id": str(uuid.uuid4())}
        first = self.client.post("/api/v1/admin/coupons/", payload, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        second = self.client.post("/api/v1/admin/coupons/", payload, format="json")
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(first.data["data"]["public_id"], second.data["data"]["public_id"])
        self.template.is_active = False
        self.template.save(update_fields=("is_active",))
        replay = self.client.post("/api/v1/admin/coupons/", payload, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data["data"]["public_id"], replay.data["data"]["public_id"])
        new_request = {**payload, "request_id": str(uuid.uuid4())}
        self.assertEqual(self.client.post("/api/v1/admin/coupons/", new_request, format="json").status_code, 400)
        payload["user_public_id"] = str(self.provider_user.public_id)
        self.assertEqual(self.client.post("/api/v1/admin/coupons/", payload, format="json").status_code, 400)
        self.assertEqual(UserCoupon.objects.count(), 1)

    def test_preview_template_cannot_be_deleted(self):
        self.preview()
        response = self.client.delete(f"/api/v1/admin/coupon-templates/{self.template.public_id}/")
        self.assertEqual(response.status_code, 400, response.data)


class CommissionAndGenderTests(APITestCase):
    def test_gender_is_only_male_or_female_when_written_but_legacy_unset_can_edit_name(self):
        user = User.objects.create_user(phone="13977220000")
        self.client.force_authenticate(user)
        self.assertEqual(self.client.patch("/api/v1/users/me/", {"nickname": "新昵称"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch("/api/v1/users/me/", {"gender": "unspecified"}, format="json").status_code, 400)
        for value in ("male", "female"):
            self.assertEqual(self.client.patch("/api/v1/users/me/", {"gender": value}, format="json").status_code, 200)

    def test_effective_commission_matches_snapshot_and_caps_bonus_per_category(self):
        user = User.objects.create_user(phone="13977220001")
        provider = ProviderProfile.objects.create(user=user)
        for name, rate in (("桌球", 30), ("电竞", 5)):
            category = ServiceCategory.objects.create(name=name, slug=f"test-{rate}", platform_commission_rate=rate)
            ProviderCategoryGrant.objects.create(provider=provider, category=category)
        rules = {"provider_commission_reset_period": "month", "provider_commission_tiers": [
            {"threshold_amount": 0, "bonus_rate": "10"}, {"threshold_amount": 100000, "bonus_rate": "15"},
        ]}
        with patch("backoffice.operation_settings.platform_operation_rules", return_value=rules):
            data = provider_commission_overview(provider)
            self.assertEqual(data["tiers_source"], "platform")
            self.assertEqual(data["current_tier_index"], 0)
            self.assertEqual(data["next_tier_remaining_amount"], 100000)
            rows = {row["category_name"]: row for row in data["categories"]}
            self.assertEqual(Decimal(rows["桌球"]["provider_rate"]), 80)
            self.assertEqual(Decimal(rows["电竞"]["provider_rate"]), 100)
            self.assertEqual(rows["桌球"]["platform_commission_rate"], commission_snapshot(provider, Decimal("30.00"))["platform_commission_rate"])
            provider.commission_tiers_override = []
            provider.commission_reset_period_override = "year"
            own = provider_commission_overview(provider)
            self.assertEqual(own["tiers_source"], "provider")
            self.assertEqual(own["provider_bonus_period"], "year")
            self.assertEqual(own["tiers"], [])
