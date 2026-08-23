from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from mediafiles.models import MediaAsset
from providers.models import (
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)
from locations.tencent import RouteResult

from .models import ProviderOrder


@override_settings(DEBUG=True)
class ProviderOrderApiTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(phone="13800000101", password="test")
        self.provider_user = User.objects.create_user(
            phone="13800000102", password="test", nickname="晓晓"
        )
        self.provider = ProviderProfile.objects.create(
            user=self.provider_user,
            status=ProviderProfile.Status.APPROVED,
            is_accepting_orders=True,
            service_city_code="130400",
            service_city_name="邯郸市",
            source_longitude=Decimal("114.4907000"),
            source_latitude=Decimal("36.6123000"),
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
        self.route_patcher = patch(
            "orders.serializers.tencent_map.driving_route",
            return_value=RouteResult(distance_km=Decimal("6.80"), duration_minutes=18),
        )
        self.route_patcher.start()
        self.addCleanup(self.route_patcher.stop)

    def payload(self):
        starts_at = (timezone.localtime() + timedelta(days=1)).replace(
            hour=13, minute=0, second=0, microsecond=0
        )
        return {
            "service_id": self.service.id,
            "starts_at": starts_at.isoformat(),
            "duration_minutes": 120,
            "meeting_address": "邯郸市丛台区美乐城南门",
            "longitude": "114.5060000",
            "latitude": "36.6200000",
            "contact_name": "张三",
            "contact_phone": "13812346688",
            "note": "请提前联系",
        }

    def create_paid_accepted_order(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        self.client.force_login(self.provider_user)
        self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")
        return ProviderOrder.objects.get(order_no=order_no)

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

    def test_provider_accepts_paid_order_and_customer_sees_pending_service(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        paid = self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        self.assertEqual(paid.status_code, 200)

        self.client.force_login(self.provider_user)
        provider_orders = self.client.get("/api/v1/providers/me/orders/")
        self.assertEqual(provider_orders.status_code, 200)
        self.assertEqual(
            [item["order_no"] for item in provider_orders.json()["data"]["items"]],
            [order_no],
        )
        provider_order = provider_orders.json()["data"]["items"][0]
        self.assertEqual(provider_order["customer_name"], self.customer.nickname)
        self.assertIsNotNone(provider_order["acceptance_expires_at"])

        accepted = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["data"]["status"], ProviderOrder.Status.PENDING_SERVICE)
        self.assertIsNotNone(accepted.json()["data"]["accepted_at"])

        repeated = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(
            repeated.json()["data"]["accepted_at"],
            accepted.json()["data"]["accepted_at"],
        )

        self.client.force_login(self.customer)
        customer_order = self.client.get(f"/api/v1/provider-orders/{order_no}/")
        self.assertEqual(
            customer_order.json()["data"]["status"],
            ProviderOrder.Status.PENDING_SERVICE,
        )

    def test_pending_review_order_is_counted_in_customer_overview(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order = ProviderOrder.objects.get(order_no=create.json()["data"]["order_no"])
        order.status = ProviderOrder.Status.PENDING_REVIEW
        order.save(update_fields=("status", "updated_at"))

        overview = self.client.get("/api/v1/users/me/overview/")

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["data"]["pending_review_count"], 1)
        self.assertEqual(overview.json()["data"]["after_sales_count"], 0)

    @patch("orders.serializers.build_media_url", return_value="https://media.test/arrival.webp")
    def test_provider_fulfills_order_and_customer_confirms_completion(self, _build_url):
        order = self.create_paid_accepted_order()

        departed = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/depart/")
        self.assertEqual(departed.status_code, 200)
        self.assertEqual(departed.json()["data"]["status"], ProviderOrder.Status.DEPARTED)
        self.assertIsNotNone(departed.json()["data"]["departed_at"])

        missing_photo = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/start/"
        )
        self.assertEqual(missing_photo.status_code, 400)

        photo = MediaAsset.objects.create(
            owner=self.provider_user,
            scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.ORDER_EVIDENCE,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"dazzy-test/private/order-evidence/{self.provider_user.public_id}/arrival.webp",
            content_type="image/webp",
        )
        evidence = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/arrival-evidence/",
            {
                "photo_id": str(photo.pk),
                "longitude": "114.5060000",
                "latitude": "36.6200000",
                "accuracy_m": "12.50",
            },
            content_type="application/json",
        )
        self.assertEqual(evidence.status_code, 200)
        self.assertEqual(evidence.json()["data"]["arrival_photo_url"], "https://media.test/arrival.webp")

        started = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/start/")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["data"]["status"], ProviderOrder.Status.IN_SERVICE)
        self.assertIsNotNone(started.json()["data"]["service_started_at"])

        completed = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/complete/"
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(
            completed.json()["data"]["status"], ProviderOrder.Status.PENDING_CONFIRMATION
        )
        self.assertIsNotNone(completed.json()["data"]["completion_submitted_at"])

        self.client.force_login(self.customer)
        confirmed = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/confirm-completion/"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["data"]["status"], ProviderOrder.Status.PENDING_REVIEW)
        self.assertIsNotNone(confirmed.json()["data"]["customer_confirmed_at"])

        repeated = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/confirm-completion/"
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(
            repeated.json()["data"]["customer_confirmed_at"],
            confirmed.json()["data"]["customer_confirmed_at"],
        )

        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_REVIEW)
        self.assertEqual(order.arrival_photo, photo)
        self.assertEqual(order.arrival_longitude, Decimal("114.5060000"))
        self.assertEqual(order.arrival_latitude, Decimal("36.6200000"))

    def test_fulfillment_rejects_invalid_sequence_and_foreign_photo(self):
        order = self.create_paid_accepted_order()

        start_before_departure = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/start/"
        )
        self.assertEqual(start_before_departure.status_code, 400)
        self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/depart/")

        foreign_photo = MediaAsset.objects.create(
            owner=self.customer,
            scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.ORDER_EVIDENCE,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"dazzy-test/private/order-evidence/{self.customer.public_id}/foreign.webp",
        )
        evidence = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/arrival-evidence/",
            {
                "photo_id": str(foreign_photo.pk),
                "longitude": "114.5060000",
                "latitude": "36.6200000",
            },
            content_type="application/json",
        )

        self.assertEqual(evidence.status_code, 400)
        order.refresh_from_db()
        self.assertIsNone(order.arrival_photo_id)

    def test_provider_cannot_see_unpaid_orders(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]

        self.client.force_login(self.provider_user)
        provider_orders = self.client.get("/api/v1/providers/me/orders/")
        detail = self.client.get(f"/api/v1/providers/me/orders/{order_no}/")

        self.assertEqual(provider_orders.status_code, 200)
        self.assertEqual(provider_orders.json()["data"]["items"], [])
        self.assertEqual(detail.status_code, 404)

    def test_provider_cannot_accept_after_thirty_minute_deadline(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        ProviderOrder.objects.filter(order_no=order_no).update(
            paid_at=timezone.now() - timedelta(minutes=31)
        )

        self.client.force_login(self.provider_user)
        response = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            ProviderOrder.objects.get(order_no=order_no).status,
            ProviderOrder.Status.PENDING_ACCEPTANCE,
        )

    def test_order_creation_requires_provider_to_be_accepting_orders(self):
        self.provider.is_accepting_orders = False
        self.provider.save(update_fields=("is_accepting_orders",))

        response = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)
