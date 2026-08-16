from datetime import time, timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from accounts.models import User

from .models import ProviderProfile, ProviderService, ProviderWeeklyAvailability, ServiceCategory


class ProviderModelTests(TestCase):
    def test_provider_service_uses_integer_minor_units_and_wgs84_point(self):
        user = User.objects.create_user(phone="13800000001", password="test-password")
        provider = ProviderProfile.objects.create(
            user=user,
            source_longitude=Decimal("116.4039810"),
            source_latitude=Decimal("39.9150010"),
            service_center=Point(116.397755, 39.913873, srid=4326),
        )
        category = ServiceCategory.objects.create(name="台球陪玩", slug="billiards")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        self.assertEqual(service.price_amount, 17800)
        self.assertEqual(provider.service_center.srid, 4326)
        self.assertEqual(provider.source_longitude, Decimal("116.4039810"))

    def test_provider_list_supports_gcj02_distance(self):
        user = User.objects.create_user(
            phone="13800000003", password="test-password", nickname="晓晓"
        )
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="110100",
            service_city_name="北京市",
            service_center=Point(116.397755, 39.913873, srid=4326),
        )
        category = ServiceCategory.objects.create(name="台球陪玩", slug="billiards-list")
        ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        response = self.client.get(
            "/api/v1/providers/",
            {
                "city_code": "110100",
                "longitude": "116.4039810",
                "latitude": "39.9150010",
                "ordering": "distance",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(response.json()["data"]["items"][0]["nickname"], "晓晓")
        self.assertIsNotNone(response.json()["data"]["items"][0]["distance_km"])

    def test_provider_detail_returns_public_profile_and_active_services(self):
        user = User.objects.create_user(
            phone="13800000004",
            password="test-password",
            nickname="可可",
            birth_date="2000-05-23",
        )
        provider = ProviderProfile.objects.create(
            user=user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="110100",
            service_city_name="北京市",
            bio="喜欢旅行与摄影",
        )
        category = ServiceCategory.objects.create(name="旅行陪伴", slug="travel-detail")
        ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )

        response = self.client.get(f"/api/v1/providers/{user.public_id}/")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["nickname"], "可可")
        self.assertEqual(data["services"][0]["price_amount"], 17800)
        self.assertEqual(data["birth_date"], "2000-05-23")

    def test_provider_detail_hides_unapproved_profile(self):
        user = User.objects.create_user(phone="13800000005", password="test-password")
        ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.DRAFT)

        response = self.client.get(f"/api/v1/providers/{user.public_id}/")

        self.assertEqual(response.status_code, 404)

    def test_availability_returns_only_configured_future_slots(self):
        user = User.objects.create_user(phone="13800000006", password="test", nickname="小雨")
        provider = ProviderProfile.objects.create(user=user, status=ProviderProfile.Status.APPROVED)
        category = ServiceCategory.objects.create(name="摄影陪伴", slug="photo-availability")
        service = ProviderService.objects.create(
            provider=provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=21800,
        )
        day = timezone.localdate() + timedelta(days=1)
        ProviderWeeklyAvailability.objects.create(
            provider=provider,
            weekday=day.weekday(),
            starts_at=time(13),
            ends_at=time(17),
        )

        response = self.client.get(
            f"/api/v1/providers/{user.public_id}/availability/",
            {"service_id": service.id, "start_date": day.isoformat(), "days": 1},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["earliest"]["starts_at"][11:16], "13:00")
        self.assertEqual(len(data["dates"][0]["slots"]), 5)

    def test_availability_rejects_service_owned_by_another_provider(self):
        first_user = User.objects.create_user(phone="13800000007", password="test")
        second_user = User.objects.create_user(phone="13800000008", password="test")
        ProviderProfile.objects.create(user=first_user, status=ProviderProfile.Status.APPROVED)
        second = ProviderProfile.objects.create(
            user=second_user, status=ProviderProfile.Status.APPROVED
        )
        category = ServiceCategory.objects.create(name="商务陪同", slug="business-availability")
        service = ProviderService.objects.create(
            provider=second,
            category=category,
            billing_type=ProviderService.BillingType.PER_SESSION,
            price_amount=26800,
            estimated_duration_minutes=120,
        )

        response = self.client.get(
            f"/api/v1/providers/{first_user.public_id}/availability/",
            {"service_id": service.id},
        )

        self.assertEqual(response.status_code, 404)
