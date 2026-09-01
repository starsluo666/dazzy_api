import uuid
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from backoffice.models import PlatformOperationSetting
from mediafiles.models import MediaAsset
from providers.models import (
    ProviderLiveLocation,
    ProviderProfile,
    ProviderService,
    ProviderWeeklyAvailability,
    ServiceCategory,
)
from locations.tencent import RouteResult
from locations.models import UserAddress
from taskcenter.models import ScheduledTask

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
        )
        now = timezone.now()
        self.live_location = ProviderLiveLocation.objects.create(
            provider=self.provider,
            session_id=uuid.uuid4(),
            source_longitude=Decimal("114.4907000"),
            source_latitude=Decimal("36.6123000"),
            position=Point(114.4907, 36.6123, srid=4326),
            accuracy_m=Decimal("12.00"),
            located_at=now,
            received_at=now,
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
        self.address = UserAddress.objects.create(
            user=self.customer,
            name="邯郸美乐城",
            address="人民东路456号",
            city_name="邯郸市",
            contact_name="张三",
            contact_gender=UserAddress.ContactGender.MR,
            contact_phone="13812346688",
            longitude="114.5060000",
            latitude="36.6200000",
            is_default=True,
        )
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
            "address_id": self.address.id,
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

    def test_inactive_service_category_cannot_be_booked(self):
        category = self.service.category
        category.is_active = False
        category.save(update_fields=("is_active", "updated_at"))

        response = self.client.post(
            "/api/v1/provider-orders/preview/",
            self.payload(),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("服务不存在或不可预约", response.json()["service_id"][0])

    def test_create_locks_slot_and_simulated_payment_moves_to_pending_acceptance(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        self.assertEqual(create.status_code, 201)
        order_no = create.json()["data"]["order_no"]
        self.assertEqual(create.json()["data"]["status"], ProviderOrder.Status.PENDING_PAYMENT)
        self.assertEqual(create.json()["data"]["meeting_location_name"], "邯郸美乐城")
        self.assertEqual(create.json()["data"]["contact_gender_label"], "先生")
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
                business_key=order_no,
                status=ScheduledTask.Status.PENDING,
            ).exists()
        )

        conflict = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        self.assertEqual(conflict.status_code, 400)

        paid = self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid.json()["data"]["status"], ProviderOrder.Status.PENDING_ACCEPTANCE)
        self.assertIsNotNone(paid.json()["data"]["acceptance_expires_at"])
        self.assertEqual(
            ScheduledTask.objects.get(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
                business_key=order_no,
            ).status,
            ScheduledTask.Status.CANCELLED,
        )
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
                business_key=order_no,
                status=ScheduledTask.Status.PENDING,
            ).exists()
        )

    def test_payment_and_acceptance_task_transition_is_atomic(self):
        created = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = created.json()["data"]["order_no"]

        with patch(
            "orders.views.register_provider_acceptance_timeout",
            side_effect=RuntimeError("task registration failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")

        order = ProviderOrder.objects.get(order_no=order_no)
        payment_task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
            business_key=order_no,
        )
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_PAYMENT)
        self.assertIsNone(order.paid_at)
        self.assertEqual(payment_task.status, ScheduledTask.Status.PENDING)
        self.assertFalse(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
                business_key=order_no,
            ).exists()
        )

    def test_create_uses_configured_payment_timeout(self):
        PlatformOperationSetting.objects.create(provider_order_payment_timeout_minutes=25)
        created_after = timezone.now()

        response = self.client.post(
            "/api/v1/provider-orders/",
            self.payload(),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        order = ProviderOrder.objects.get(order_no=response.json()["data"]["order_no"])
        self.assertGreaterEqual(
            order.payment_expires_at,
            created_after + timedelta(minutes=25),
        )
        self.assertLess(
            order.payment_expires_at,
            created_after + timedelta(minutes=25, seconds=2),
        )

    def test_order_uses_owned_address_and_keeps_snapshot(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        self.assertEqual(create.status_code, 201)
        order = ProviderOrder.objects.get(order_no=create.json()["data"]["order_no"])

        self.address.contact_name = "修改后联系人"
        self.address.address = "修改后的详细地址"
        self.address.save(update_fields=("contact_name", "address", "updated_at"))

        order.refresh_from_db()
        self.assertEqual(order.contact_name, "张三")
        self.assertEqual(order.meeting_address, "人民东路456号")

        other_user = User.objects.create_user(phone="13800000103", password="test")
        other_address = UserAddress.objects.create(
            user=other_user,
            name="他人地址",
            address="测试路1号",
            contact_name="他人",
            contact_gender=UserAddress.ContactGender.MS,
            contact_phone="13912346688",
            longitude="114.5060000",
            latitude="36.6200000",
        )
        payload = self.payload()
        payload["address_id"] = other_address.id
        denied = self.client.post(
            "/api/v1/provider-orders/preview/", payload, content_type="application/json"
        )
        self.assertEqual(denied.status_code, 400)

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
        self.assertEqual(
            ProviderOrder.objects.get(order_no=order_no).route_distance_km, Decimal("6.80")
        )
        self.assertEqual(
            ScheduledTask.objects.get(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY,
                business_key=order_no,
            ).status,
            ScheduledTask.Status.CANCELLED,
        )

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
        self.assertEqual(
            ScheduledTask.objects.get(business_key=order.order_no).status,
            ScheduledTask.Status.SUCCEEDED,
        )

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
        self.assertEqual(provider_order["contact_phone_display"], "138****6688")
        self.assertIsNone(provider_order["meeting_longitude"])
        self.assertIsNotNone(provider_order["acceptance_expires_at"])

        accepted = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["data"]["status"], ProviderOrder.Status.PENDING_SERVICE)
        self.assertIsNotNone(accepted.json()["data"]["accepted_at"])
        self.assertEqual(accepted.json()["data"]["contact_phone_display"], "13812346688")
        self.assertAlmostEqual(
            float(accepted.json()["data"]["meeting_longitude"]),
            114.506,
        )
        self.assertEqual(
            ScheduledTask.objects.get(
                task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
                business_key=order_no,
            ).status,
            ScheduledTask.Status.CANCELLED,
        )

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

    def test_provider_rejects_paid_order_to_support_with_reason(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        self.client.force_login(self.provider_user)

        missing_reason = self.client.post(
            f"/api/v1/providers/me/orders/{order_no}/reject/",
            {"reason": ""},
            content_type="application/json",
        )
        self.assertEqual(missing_reason.status_code, 400)

        rejected = self.client.post(
            f"/api/v1/providers/me/orders/{order_no}/reject/",
            {"reason": "档期临时冲突"},
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["data"]["status"], ProviderOrder.Status.PENDING_SUPPORT)
        self.assertEqual(rejected.json()["data"]["provider_rejection_reason"], "档期临时冲突")
        self.assertIsNotNone(rejected.json()["data"]["provider_rejected_at"])
        self.assertEqual(rejected.json()["data"]["contact_phone_display"], "138****6688")

        repeated = self.client.post(
            f"/api/v1/providers/me/orders/{order_no}/reject/",
            {"reason": "重复提交不会覆盖"},
            content_type="application/json",
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(
            repeated.json()["data"]["provider_rejection_reason"],
            "档期临时冲突",
        )

        cannot_accept = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")
        self.assertEqual(cannot_accept.status_code, 400)

        self.client.force_login(self.customer)
        customer_detail = self.client.get(f"/api/v1/provider-orders/{order_no}/")
        self.assertEqual(customer_detail.status_code, 200)
        self.assertEqual(
            customer_detail.json()["data"]["provider_rejection_reason"],
            "档期临时冲突",
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

        missing_photo = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/start/")
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
        self.assertEqual(
            evidence.json()["data"]["arrival_photo_url"], "https://media.test/arrival.webp"
        )

        started = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/start/")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["data"]["status"], ProviderOrder.Status.IN_SERVICE)
        self.assertIsNotNone(started.json()["data"]["service_started_at"])

        completed = self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/complete/")
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(
            completed.json()["data"]["status"], ProviderOrder.Status.PENDING_CONFIRMATION
        )
        self.assertIsNotNone(completed.json()["data"]["completion_submitted_at"])
        self.assertIsNotNone(completed.json()["data"]["confirmation_expires_at"])
        self.assertIsNone(completed.json()["data"]["auto_confirmed_at"])
        confirmation_task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
            business_key=order.order_no,
        )
        self.assertEqual(confirmation_task.status, ScheduledTask.Status.PENDING)

        self.client.force_login(self.customer)
        confirmed = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/confirm-completion/"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["data"]["status"], ProviderOrder.Status.PENDING_REVIEW)
        self.assertIsNotNone(confirmed.json()["data"]["customer_confirmed_at"])
        confirmation_task.refresh_from_db()
        self.assertEqual(confirmation_task.status, ScheduledTask.Status.CANCELLED)

        repeated = self.client.post(f"/api/v1/provider-orders/{order.order_no}/confirm-completion/")
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

    def test_completion_uses_configured_confirmation_timeout_snapshot(self):
        PlatformOperationSetting.objects.create(provider_order_confirmation_timeout_days=5)
        order = self.create_paid_accepted_order()
        order.status = ProviderOrder.Status.IN_SERVICE
        order.service_started_at = timezone.now() - timedelta(hours=2)
        order.save(update_fields=("status", "service_started_at", "updated_at"))
        submitted_after = timezone.now()

        completed = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/complete/"
        )

        self.assertEqual(completed.status_code, 200)
        order.refresh_from_db()
        self.assertGreaterEqual(
            order.confirmation_expires_at,
            submitted_after + timedelta(days=5),
        )
        self.assertLess(
            order.confirmation_expires_at,
            submitted_after + timedelta(days=5, seconds=2),
        )
        original_deadline = order.confirmation_expires_at
        setting = PlatformOperationSetting.current()
        setting.provider_order_confirmation_timeout_days = 1
        setting.save(update_fields=("provider_order_confirmation_timeout_days", "updated_at"))

        repeated = self.client.post(
            f"/api/v1/providers/me/orders/{order.order_no}/complete/"
        )

        self.assertEqual(repeated.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.confirmation_expires_at, original_deadline)

    def test_completion_and_task_registration_are_atomic(self):
        order = self.create_paid_accepted_order()
        order.status = ProviderOrder.Status.IN_SERVICE
        order.service_started_at = timezone.now() - timedelta(hours=2)
        order.save(update_fields=("status", "service_started_at", "updated_at"))

        with patch(
            "orders.views.register_provider_order_confirmation_timeout",
            side_effect=RuntimeError("task registration failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/complete/")

        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.IN_SERVICE)
        self.assertIsNone(order.completion_submitted_at)
        self.assertIsNone(order.confirmation_expires_at)
        self.assertFalse(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
                business_key=order.order_no,
            ).exists()
        )

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

    def test_provider_acceptance_after_deadline_moves_order_to_support(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        ProviderOrder.objects.filter(order_no=order_no).update(
            paid_at=timezone.now() - timedelta(minutes=31),
            acceptance_expires_at=timezone.now() - timedelta(minutes=1),
        )

        self.client.force_login(self.provider_user)
        response = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            ProviderOrder.objects.get(order_no=order_no).status,
            ProviderOrder.Status.PENDING_SUPPORT,
        )

    def test_order_creation_requires_provider_to_be_accepting_orders(self):
        self.provider.is_accepting_orders = False
        self.provider.save(update_fields=("is_accepting_orders",))

        response = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)

    def test_order_creation_rejects_stale_provider_location(self):
        ProviderLiveLocation.objects.filter(pk=self.live_location.pk).update(
            received_at=timezone.now() - timedelta(minutes=31)
        )

        response = self.client.post(
            "/api/v1/provider-orders/preview/", self.payload(), content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("不在线", str(response.json()))
