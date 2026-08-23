from datetime import timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from activities.models import Activity, ActivityCategory
from providers.models import ProviderProfile, ProviderService, ServiceCategory

from .models import BrowsingHistory, ProviderFavorite


class EngagementApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="13900000101", password="test-password")
        provider_user = User.objects.create_user(phone="13900000102", nickname="收藏达人")
        self.provider = ProviderProfile.objects.create(
            user=provider_user, status=ProviderProfile.Status.APPROVED, service_city_name="邯郸市"
        )
        category = ServiceCategory.objects.create(name="收藏测试服务", slug="favorite-service")
        ProviderService.objects.create(
            provider=self.provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=12800,
        )
        self.client.force_login(self.user)

    def test_provider_favorite_is_idempotent_and_visible_in_detail(self):
        url = f"/api/v1/providers/{self.provider.user.public_id}/favorite/"
        self.assertEqual(self.client.post(url).status_code, 201)
        self.assertEqual(self.client.post(url).status_code, 201)
        self.assertEqual(ProviderFavorite.objects.filter(user=self.user).count(), 1)
        detail = self.client.get(f"/api/v1/providers/{self.provider.user.public_id}/")
        self.assertTrue(detail.json()["data"]["is_favorited"])
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(ProviderFavorite.objects.filter(user=self.user).exists())

    def test_repeated_provider_view_updates_one_history_row(self):
        url = f"/api/v1/providers/{self.provider.user.public_id}/history/"
        self.client.post(url)
        response = self.client.post(url)
        history = BrowsingHistory.objects.get(user=self.user, provider=self.provider)
        self.assertEqual(history.view_count, 2)
        self.assertEqual(response.json()["data"]["view_count"], 2)

    def test_activity_history_can_filter_and_clear(self):
        category = ActivityCategory.objects.create(name="测试活动", slug="engagement-test")
        starts_at = timezone.now() + timedelta(days=3)
        activity = Activity.objects.create(
            organizer=self.user, category=category, title="浏览活动", starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2), formation_deadline=starts_at - timedelta(hours=12),
            meeting_place_name="测试地点", meeting_address="测试地址",
            source_longitude=Decimal("114.5000000"), source_latitude=Decimal("36.6000000"),
            meeting_point=Point(114.5, 36.6, srid=4326), capacity=6, min_participants=2,
            description="活动说明", participation_rules="活动规则", aa_principal_amount=100,
            refund_template_version="standard-v1", refund_rule_snapshot={}, status=Activity.Status.RECRUITING,
        )
        self.assertEqual(self.client.post(f"/api/v1/activities/{activity.pk}/history/").status_code, 201)
        response = self.client.get("/api/v1/browsing-history/", {"type": "activity"})
        self.assertEqual(response.json()["data"]["items"][0]["target"]["title"], "浏览活动")
        self.assertEqual(self.client.delete("/api/v1/browsing-history/").status_code, 204)
        self.assertFalse(BrowsingHistory.objects.filter(user=self.user).exists())
