from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from backoffice.models import AdminAuditLog, AdminRole, Organization, OrganizationMember
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from orders.coupon_campaigns import claim_campaign, home_coupon_campaigns
from orders.coupons import coupon_payload
from orders.models import CouponCampaign, CouponCampaignClaim, CouponTemplate, UserCoupon


class CouponCampaignTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(phone="13977112000", password="test-password")
        cls.user = User.objects.create_user(phone="13977112001")
        cls.other = User.objects.create_user(phone="13977112002")
        cls.template = CouponTemplate.objects.create(name="周末好礼", face_amount=3000, min_order_amount=10000, valid_days=7)
        cls.banner = MediaAsset.objects.create(owner=cls.admin, scope="public", category="operations_image", status="uploaded", content_type="image/webp", object_key="campaign/banner.webp")

    def setUp(self):
        self.client.force_authenticate(self.admin)

    def data(self, **overrides):
        return {"name": "测试领券活动", "banner_id": str(self.banner.pk), "template_public_id": str(self.template.public_id),
                "starts_at": (timezone.now() - timedelta(hours=1)).isoformat(),
                "ends_at": (timezone.now() + timedelta(days=1)).isoformat(), "stock": 2, "sort_order": 0, **overrides}

    def create(self, **overrides):
        response = self.client.post("/api/v1/admin/coupon-campaigns/", self.data(**overrides), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return CouponCampaign.objects.get(public_id=response.data["data"]["public_id"])

    def action(self, c, action="publish", **overrides):
        result = self.client.post(f"/api/v1/admin/coupon-campaigns/{c.public_id}/", {"action": action, "revision": c.revision, "confirmed": True, **overrides}, format="json")
        c.refresh_from_db()
        return result

    def claim(self, c, user=None):
        self.client.force_authenticate(user or self.user)
        return self.client.post(f"/api/v1/coupon-campaigns/{c.public_id}/claim/", {}, format="json")

    def edit(self, c, **overrides):
        self.client.force_authenticate(self.admin)
        return self.client.patch(f"/api/v1/admin/coupon-campaigns/{c.public_id}/", self.data(**{"revision": c.revision, **overrides}), format="json")

    def test_draft_hidden_and_publish_requires_confirmation(self):
        c = self.create()
        self.assertEqual(home_coupon_campaigns(AnonymousUser()), [])
        self.assertEqual(self.action(c, confirmed=False).status_code, 400)
        self.assertEqual(self.client.get(f"/api/v1/coupon-campaigns/{c.public_id}/").status_code, 404)
        self.assertEqual(self.action(c).status_code, 200)
        self.assertEqual(len(home_coupon_campaigns(AnonymousUser())), 1)
        self.assertFalse(UserCoupon.objects.exists())
        self.assertTrue(AdminAuditLog.objects.filter(action="coupon.campaign.publish").exists())

    def test_repeat_claim_is_idempotent_including_after_offline(self):
        c = self.create()
        self.action(c)
        first = self.claim(c)
        second = self.claim(c)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["data"]["user_coupon"], second.data["data"]["user_coupon"])
        self.assertEqual(UserCoupon.objects.count(), 1)
        self.assertEqual(UserNotification.objects.filter(event_type="coupon_issued").count(), 1)
        c.refresh_from_db()
        self.assertEqual(c.issued_count, 1)
        self.client.force_authenticate(self.admin)
        self.action(c, "offline")
        self.assertEqual(self.claim(c).status_code, 200)
        self.assertEqual(self.claim(c, self.other).status_code, 400)
        self.assertEqual(UserCoupon.objects.get().status, "available")

    def test_stock_exhaustion_and_home_fallback(self):
        c = self.create(stock=1)
        self.action(c)
        self.assertEqual(self.claim(c).status_code, 200)
        self.assertEqual(self.claim(c, self.other).status_code, 400)
        self.assertEqual(home_coupon_campaigns(self.user), [])
        c.refresh_from_db()
        self.assertEqual(c.issued_count, 1)

    def test_coupon_snapshot_survives_template_edits_and_deactivation(self):
        c = self.create()
        self.action(c)
        CouponTemplate.objects.filter(pk=self.template.pk).update(name="新名字", face_amount=5000, valid_days=1, is_active=False)
        result = self.claim(c)
        self.assertEqual(result.status_code, 200)
        coupon = UserCoupon.objects.get()
        self.assertEqual(coupon.face_amount, 3000)
        self.assertEqual(coupon_payload(coupon)["template_name"], "周末好礼")
        self.assertGreater(coupon.expires_at, timezone.now() + timedelta(days=6))

    def test_window_boundary_and_future_not_advertised(self):
        c = self.create(starts_at=(timezone.now() + timedelta(hours=1)).isoformat())
        self.action(c)
        self.assertEqual(self.claim(c).status_code, 400)
        self.assertEqual(home_coupon_campaigns(self.user), [])
        now = timezone.now()
        CouponCampaign.objects.filter(pk=c.pk).update(starts_at=now - timedelta(days=1), ends_at=now)
        with patch("orders.coupon_campaigns.timezone.now", return_value=now):
            self.assertEqual(self.claim(c).status_code, 400)

    def test_cannot_change_published_template_or_reduce_stock_below_issued(self):
        c = self.create()
        self.action(c)
        self.claim(c)
        self.claim(c, self.other)
        c.refresh_from_db()
        self.assertEqual(self.edit(c, stock=1).status_code, 400)
        other = CouponTemplate.objects.create(name="另一种券", face_amount=1000)
        self.assertEqual(self.edit(c, template_public_id=str(other.public_id)).status_code, 400)
        self.assertEqual(self.edit(c, stock=3).status_code, 200)
        self.assertEqual(self.claim(c, self.other).status_code, 200)
        self.assertEqual(UserCoupon.objects.count(), 2)

    def test_stale_revision_rejected(self):
        c = self.create()
        self.action(c)
        self.assertEqual(self.edit(c, revision=1).status_code, 400)
        self.assertEqual(self.action(c, "offline", revision=1).status_code, 400)

    def test_banner_reference_protects_soft_deletion_and_template_delete(self):
        c = self.create()
        result = self.client.post("/api/v1/admin/assets/batch-delete/", {"ids": [str(self.banner.pk)]}, format="json")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["data"]["deleted"], [])
        self.assertEqual(result.data["data"]["blocked"][0]["references"][0]["type"], "coupon_campaign")
        self.assertEqual(self.client.delete(f"/api/v1/admin/coupon-templates/{self.template.public_id}/").status_code, 400)
        MediaAsset.objects.filter(pk=self.banner.pk).update(status="deleted")
        self.assertEqual(self.action(c).status_code, 400)

    def test_private_customer_or_icon_assets_rejected(self):
        for overrides in ({"scope": "private"}, {"category": "provider_photo"}, {"category": "operations_icon"}, {"status": "deleted"}, {"content_type": "video/mp4"}):
            with self.subTest(overrides=overrides):
                MediaAsset.objects.filter(pk=self.banner.pk).update(scope="public", category="operations_image", status="uploaded", content_type="image/webp")
                MediaAsset.objects.filter(pk=self.banner.pk).update(**overrides)
                self.assertEqual(self.client.post("/api/v1/admin/coupon-campaigns/", self.data(), format="json").status_code, 400)

    def test_unauthenticated_and_restricted_accounts_cannot_claim(self):
        c = self.create()
        self.action(c)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(f"/api/v1/coupon-campaigns/{c.public_id}/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/coupon-campaigns/{c.public_id}/claim/").status_code, 401)
        self.assertEqual(self.claim(c, self.admin).status_code, 400)
        User.objects.filter(pk=self.user.pk).update(account_status="restricted")
        self.assertEqual(self.claim(c).status_code, 400)

    def test_admin_permission_and_region_scope(self):
        for scope, org_type, permissions, status in (
            ("all", "platform", ["coupon_campaign.view"], 200),
            ("city", "platform", ["coupon_campaign.view", "coupon_campaign.manage"], 403),
            ("all", "city_agent", ["coupon_campaign.view", "coupon_campaign.manage"], 403),
            ("all", "platform", ["coupon.view", "coupon.issue"], 403),
        ):
            OrganizationMember.objects.filter(user=self.other).delete()
            organization = Organization.objects.create(name="测试", code=f"org{AdminRole.objects.count()}", organization_type=org_type)
            role = AdminRole.objects.create(name="测试", code="test", organization=organization, data_scope=scope, permissions=permissions)
            OrganizationMember.objects.create(user=self.other, organization=organization, role=role)
            self.client.force_authenticate(self.other)
            self.assertEqual(self.client.get("/api/v1/admin/coupon-campaigns/").status_code, status)
            self.assertEqual(self.client.post("/api/v1/admin/coupon-campaigns/", self.data(), format="json").status_code, 403)

    def test_claim_records_are_masked_and_only_own_claim_is_public(self):
        c = self.create()
        self.action(c)
        self.claim(c)
        self.client.force_authenticate(self.other)
        result = self.client.get(f"/api/v1/coupon-campaigns/{c.public_id}/")
        self.assertIsNone(result.data["data"]["user_coupon"])
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.get(f"/api/v1/admin/coupon-campaigns/{c.public_id}/claims/").status_code, 403)
        self.client.force_authenticate(self.admin)
        result = self.client.get(f"/api/v1/admin/coupon-campaigns/{c.public_id}/claims/")
        self.assertEqual(result.data["data"]["pagination"]["total"], 1)
        self.assertIn("****", result.data["data"]["items"][0]["phone_masked"])

    def test_claim_and_notification_roll_back_together(self):
        c = self.create()
        self.action(c)
        with patch("notifications.services.create_notification", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                claim_campaign(campaign_id=c.public_id, user=self.user)
        c.refresh_from_db()
        self.assertEqual(c.issued_count, 0)
        self.assertFalse(UserCoupon.objects.exists())
        self.assertFalse(CouponCampaignClaim.objects.exists())

    def test_database_guards_stock_and_duplicate_claim(self):
        c = self.create()
        self.action(c)
        self.claim(c)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CouponCampaign.objects.filter(pk=c.pk).update(issued_count=99)
        coupon = UserCoupon.objects.create(owner=self.user, face_amount=100, min_order_amount=0, expires_at=timezone.now() + timedelta(days=1))
        with self.assertRaises(IntegrityError), transaction.atomic():
            CouponCampaignClaim.objects.create(campaign=c, user=self.user, coupon=coupon)

    def test_multiple_campaigns_are_independent_and_sorted(self):
        first = self.create(sort_order=20)
        second = self.create(sort_order=10)
        self.action(first)
        self.action(second)
        self.assertEqual([c["public_id"] for c in home_coupon_campaigns(self.user)], [str(second.public_id), str(first.public_id)])
        self.claim(first)
        self.claim(second)
        self.assertEqual(UserCoupon.objects.count(), 2)
        self.assertTrue(all(c["claimed"] for c in home_coupon_campaigns(self.user)))

    @patch("home.views._recommended_providers", return_value=[])
    @patch("home.views._recommended_activities", return_value=[])
    @patch("home.views.build_home_card_assets", return_value={})
    def test_home_includes_campaigns_without_exposing_admin_fields(self, *_mocks):
        c = self.create()
        self.action(c)
        self.client.force_authenticate(None)
        result = self.client.get("/api/v1/home/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.data["data"]["coupon_campaigns"]), 1)
        campaign = result.data["data"]["coupon_campaigns"][0]
        self.assertNotIn("stock", campaign)
        self.assertNotIn("revision", campaign)
        self.assertIsNone(campaign["user_coupon"])
        self.assertEqual(result["Cache-Control"], "private, no-store")

    def test_used_expired_or_revoked_coupon_cannot_be_claimed_again(self):
        c = self.create()
        self.action(c)
        self.claim(c)
        for status in ("used", "revoked", "available"):
            UserCoupon.objects.update(status=status, expires_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(self.claim(c).status_code, 200)
            self.assertEqual(UserCoupon.objects.count(), 1)
