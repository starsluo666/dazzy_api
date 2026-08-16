from decimal import Decimal

from django.contrib.gis.geos import Point
from django.test import TestCase

from accounts.models import User

from .models import ProviderProfile, ProviderService, ServiceCategory


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
