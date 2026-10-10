"""Run on disposable PostgreSQL/PostGIS: SQLite does not implement row locks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
from .coupon_batches import template_snapshot
from .coupon_campaigns import claim_campaign
from .models import CouponCampaign, CouponCampaignClaim, CouponTemplate, UserCoupon


@skipUnless(connection.vendor == "postgresql", "Needs PostgreSQL row locks")
class CouponCampaignConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.users = [User.objects.create_user(phone=f"1397711220{i}") for i in range(2)]
        template = CouponTemplate.objects.create(name="并发测试券", face_amount=100, valid_days=1)
        banner = MediaAsset.objects.create(owner=self.users[0], scope="public", category="operations_image", status="uploaded", content_type="image/webp", object_key="synthetic/concurrency.webp")
        self.campaign = CouponCampaign.objects.create(
            name="并发活动", banner=banner, template=template, template_snapshot=template_snapshot(template),
            starts_at=timezone.now() - timedelta(hours=1), ends_at=timezone.now() + timedelta(hours=1),
            stock=1, status="published", published_at=timezone.now(), created_by=self.users[0], updated_by=self.users[0],
        )

    def race(self, users):
        barrier = Barrier(2)

        def run(user):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return claim_campaign(campaign_id=self.campaign.public_id, user=user)["user_coupon"]["public_id"]
                except ValidationError:
                    return "exhausted"
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, user) for user in users]
            return [future.result(timeout=20) for future in futures]

    def test_last_coupon_is_not_oversold(self):
        results = self.race(self.users)
        self.assertEqual(results.count("exhausted"), 1)
        self.assertEqual(UserCoupon.objects.count(), 1)
        self.assertEqual(CouponCampaignClaim.objects.count(), 1)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.issued_count, 1)

    def test_concurrent_duplicate_claim_returns_same_coupon_and_one_notice(self):
        results = self.race([self.users[0], self.users[0]])
        self.assertEqual(results[0], results[1])
        self.assertNotEqual(results[0], "exhausted")
        self.assertEqual(UserCoupon.objects.count(), 1)
        self.assertEqual(UserNotification.objects.filter(event_type="coupon_issued").count(), 1)
