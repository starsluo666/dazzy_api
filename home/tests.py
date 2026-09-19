import uuid
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from activities.models import Activity, ActivityCategory
from mediafiles.models import MediaAsset
from providers.models import (
    ProviderProfile,
    ProviderLiveLocation,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)


class HomeDiscoveryTests(TestCase):
    def setUp(self):
        provider_category = ServiceCategory.objects.create(name="台球陪玩", slug="home-billiards")
        activity_category = ActivityCategory.objects.create(name="桌游", slug="home-board-games")
        tomorrow = timezone.localdate() + timedelta(days=1)

        for index in range(5):
            user = User.objects.create_user(
                phone=f"1391000000{index}",
                password="test",
                nickname=f"达人{index}",
            )
            lifestyle_photo = MediaAsset.objects.create(
                owner=user,
                scope=MediaAsset.Scope.PUBLIC,
                category=MediaAsset.Category.PROVIDER_PHOTO,
                status=MediaAsset.Status.UPLOADED,
                object_key=f"public/provider-photos/{user.public_id}/home-{index}.webp",
            )
            provider = ProviderProfile.objects.create(
                user=user,
                status=ProviderProfile.Status.APPROVED,
                identity_status=ProviderProfile.IdentityStatus.VERIFIED,
                lifestyle_photo=lifestyle_photo,
                bio="用于首页推荐测试的完整达人资料。",
                service_city_code="130400",
                service_city_name="邯郸市",
                is_accepting_orders=True,
                rating=Decimal("4.90") - Decimal(index) / 100,
                service_count=20 - index,
            )
            now = timezone.now()
            ProviderLiveLocation.objects.create(
                provider=provider,
                session_id=uuid.uuid4(),
                source_longitude=Decimal("114.5240070") + Decimal(index) / 1000,
                source_latitude=Decimal("36.6074460"),
                position=Point(114.518 + index * 0.001, 36.607, srid=4326),
                accuracy_m=Decimal("12.00"),
                located_at=now,
                received_at=now,
            )
            ProviderService.objects.create(
                provider=provider,
                category=provider_category,
                billing_type=ProviderService.BillingType.HOURLY,
                price_amount=16800,
            )
            ProviderWeeklyAvailability.objects.create(
                provider=provider,
                weekday=tomorrow.weekday(),
                starts_at=time(13),
                ends_at=time(18),
            )

        organizer = User.objects.create_user(phone="13920000000", password="test")
        for index in range(4):
            starts_at = timezone.now() + timedelta(days=index + 2)
            Activity.objects.create(
                organizer=organizer,
                category=activity_category,
                title=f"活动{index}",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=3),
                formation_deadline=starts_at - timedelta(hours=12),
                meeting_place_name="测试场馆",
                meeting_address="邯郸市测试地址",
                city_code="130400",
                city_name="邯郸市",
                source_longitude=Decimal("114.5240070") + Decimal(index) / 1000,
                source_latitude=Decimal("36.6074460"),
                meeting_point=Point(114.524 + index * 0.001, 36.607, srid=4326),
                capacity=8,
                min_participants=4,
                description="测试活动",
                participation_rules="准时到场",
                aa_principal_amount=6800,
                refund_template_version="standard-v1",
                refund_rule_snapshot={"version": "standard-v1"},
                status=Activity.Status.RECRUITING,
            )

    @patch("home.views.build_home_card_assets")
    def test_home_returns_bounded_aggregated_sections(self, build_assets):
        build_assets.return_value = {
            "provider_companion_url": "https://media.test/provider.webp",
            "group_activity_url": "https://media.test/activity.webp",
        }

        response = self.client.get(
            "/api/v1/home/",
            {
                "city_code": "130400",
                "longitude": "114.5240070",
                "latitude": "36.6074460",
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data["recommended_providers"]), 4)
        self.assertEqual(len(data["recommended_activities"]), 3)
        self.assertEqual(data["card_assets"]["provider_companion_url"], "https://media.test/provider.webp")
        provider = data["recommended_providers"][0]
        self.assertEqual(provider["availability_status"], "available")
        self.assertIsNotNone(provider["earliest_available_at"])
        self.assertFalse(provider["is_favorited"])
        activity = data["recommended_activities"][0]
        self.assertEqual(activity["participant_count"], 0)
        self.assertIsNotNone(activity["distance_km"])
        self.assertTrue(
            all(item["city_code"] == "130400" for item in data["recommended_activities"])
        )

    @patch("home.views.build_home_card_assets", return_value={})
    def test_home_activity_recommendations_follow_selected_city(self, _build_assets):
        source = Activity.objects.first()
        Activity.objects.create(
            organizer=source.organizer,
            category=source.category,
            title="北京活动",
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=3),
            formation_deadline=timezone.now() + timedelta(hours=12),
            meeting_place_name="北京测试场馆",
            meeting_address="北京市测试地址",
            city_code="110100",
            city_name="北京市",
            source_longitude=Decimal("116.4039810"),
            source_latitude=Decimal("39.9150010"),
            meeting_point=Point(116.397755, 39.913873, srid=4326),
            capacity=8,
            min_participants=4,
            description="跨城市过滤测试",
            participation_rules="准时到场",
            aa_principal_amount=6800,
            refund_template_version="standard-v1",
            refund_rule_snapshot={"version": "standard-v1"},
            status=Activity.Status.RECRUITING,
        )

        response = self.client.get("/api/v1/home/", {"city_code": "110100"})

        self.assertEqual(response.status_code, 200)
        items = response.json()["data"]["recommended_activities"]
        self.assertEqual([item["title"] for item in items], ["北京活动"])
        self.assertTrue(all(item["city_code"] == "110100" for item in items))

    def test_home_rejects_incomplete_coordinates(self):
        response = self.client.get("/api/v1/home/", {"longitude": "114.5240070"})

        self.assertEqual(response.status_code, 400)

    @patch("home.views.build_home_card_assets", return_value={})
    def test_home_keeps_offline_providers_with_last_known_distance(self, _build_assets):
        ProviderLiveLocation.objects.update(
            received_at=timezone.now() - timedelta(minutes=31)
        )

        response = self.client.get(
            "/api/v1/home/",
            {
                "city_code": "130400",
                "longitude": "114.5240070",
                "latitude": "36.6074460",
            },
        )

        self.assertEqual(response.status_code, 200)
        providers = response.json()["data"]["recommended_providers"]
        self.assertEqual(len(providers), 4)
        self.assertTrue(all(not provider["is_online"] for provider in providers))
        self.assertTrue(all(provider["distance_km"] is not None for provider in providers))
        self.assertTrue(
            all(provider["availability_status"] == "unavailable" for provider in providers)
        )
        self.assertTrue(all(provider["earliest_available_at"] is None for provider in providers))

    @patch("home.views.build_home_card_assets", side_effect=RuntimeError("COS unavailable"))
    def test_home_keeps_other_sections_when_assets_fail(self, _build_assets):
        response = self.client.get("/api/v1/home/", {"city_code": "130400"})

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["card_assets"], {})
        self.assertEqual(data["errors"]["card_assets"], "暂时无法加载")
        self.assertEqual(len(data["recommended_providers"]), 4)
        self.assertEqual(len(data["recommended_activities"]), 3)
