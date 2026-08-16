from datetime import time, timedelta
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from providers.models import (
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)

from .models import ProviderOrder


@override_settings(DEBUG=True)
class ProviderOrderApiTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(phone="13800000101", password="test")
        provider_user = User.objects.create_user(
            phone="13800000102", password="test", nickname="晓晓"
        )
        self.provider = ProviderProfile.objects.create(
            user=provider_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
            service_center=Point(114.4907, 36.6123, srid=4326),
        )
        category = ServiceCategory.objects.create(name="旅游陪伴", slug="travel-order")
        self.service = ProviderService.objects.create(
            provider=self.provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=17800,
        )
        ProviderWeeklyAvailability.objects.bulk_create(
            [
                ProviderWeeklyAvailability(
                    provider=self.provider, weekday=weekday, starts_at=time(9), ends_at=time(18)
                )
                for weekday in range(7)
            ]
        )
        self.client.force_login(self.customer)

    def payload(self):
        starts_at = (timezone.localtime() + timedelta(days=1)).replace(
            hour=13, minute=0, second=0, microsecond=0
        )
        return {
            "service_id": self.service.id,
            "starts_at": starts_at.isoformat(),
            "duration_minutes": 120,
            "meeting_address": "邯郸市丛台区美乐城南门",
            "route_distance_km": "6.80",
            "contact_name": "张三",
            "contact_phone": "13812346688",
            "note": "请提前联系",
        }

    def test_preview_calculates_server_side_amounts(self):
        response = self.client.post(
            "/api/v1/provider-orders/preview/", self.payload(), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["service_fee_amount"], 35600)
        self.assertEqual(data["transport_fee_amount"], 1000)
        self.assertEqual(data["discount_amount"], 0)
        self.assertEqual(data["payable_amount"], 36600)

    def test_create_locks_slot_and_simulated_payment_moves_to_pending_acceptance(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        self.assertEqual(create.status_code, 201)
        order_no = create.json()["data"]["order_no"]
        self.assertEqual(create.json()["data"]["status"], ProviderOrder.Status.PENDING_PAYMENT)

        conflict = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        self.assertEqual(conflict.status_code, 400)

        paid = self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid.json()["data"]["status"], ProviderOrder.Status.PENDING_ACCEPTANCE)

    def test_rejects_invalid_duration_and_distant_date(self):
        payload = self.payload()
        payload["duration_minutes"] = 90
        response = self.client.post(
            "/api/v1/provider-orders/preview/", payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

        payload = self.payload()
        payload["starts_at"] = (timezone.now() + timedelta(days=5)).isoformat()
        response = self.client.post(
            "/api/v1/provider-orders/preview/", payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_pending_payment_can_be_cancelled(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        response = self.client.post(f"/api/v1/provider-orders/{order_no}/cancel/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], ProviderOrder.Status.CANCELLED)
        self.assertEqual(ProviderOrder.objects.get(order_no=order_no).route_distance_km, Decimal("6.80"))

    def test_expire_command_closes_timed_out_order(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order = ProviderOrder.objects.get(order_no=create.json()["data"]["order_no"])
        order.payment_expires_at = timezone.now() - timedelta(seconds=1)
        order.save(update_fields=("payment_expires_at",))

        call_command("expire_provider_orders")

        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.CANCELLED)
        self.assertIsNotNone(order.cancelled_at)
