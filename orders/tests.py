import uuid
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User
from backoffice.models import (
    AdminAuditLog,
    PlatformOperationSetting,
    ProviderOrderAfterSalesCase,
    ProviderOrderingSetting,
)
from mediafiles.models import MediaAsset
from notifications.models import UserNotification
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

from .payment_gateway import PaymentResult
from .models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderSettlement,
)


@override_settings(DEBUG=True)
class ProviderOrderApiTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(phone="13800000101", password="test")
        self.provider_user = User.objects.create_user(
            phone="13800000102", password="test", nickname="晓晓"
        )
        lifestyle_photo = MediaAsset.objects.create(
            owner=self.provider_user,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.PROVIDER_PHOTO,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/provider-photos/{self.provider_user.public_id}/orders.webp",
        )
        self.provider = ProviderProfile.objects.create(
            user=self.provider_user,
            status=ProviderProfile.Status.APPROVED,
            identity_status=ProviderProfile.IdentityStatus.VERIFIED,
            is_accepting_orders=True,
            lifestyle_photo=lifestyle_photo,
            service_city_code="130400",
            service_city_name="邯郸市",
            bio="这是用于订单测试的已认证达人公开简介。",
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

    def test_customer_payload_hides_internal_commission_rate(self):
        preview = self.client.post(
            "/api/v1/provider-orders/preview/",
            self.payload(),
            content_type="application/json",
        )

        self.assertEqual(preview.status_code, 200)
        self.assertNotIn(
            "platform_commission_rate",
            preview.json()["data"]["pricing_snapshot"],
        )

        created = self.client.post(
            "/api/v1/provider-orders/",
            self.payload(),
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        order = ProviderOrder.objects.get(order_no=created.json()["data"]["order_no"])
        self.assertNotIn(
            "platform_commission_rate",
            created.json()["data"]["pricing_snapshot"],
        )
        self.assertIn("platform_commission_rate", order.pricing_snapshot)

        detail = self.client.get(f"/api/v1/provider-orders/{order.order_no}/")

        self.assertEqual(detail.status_code, 200)
        self.assertNotIn(
            "platform_commission_rate",
            detail.json()["data"]["pricing_snapshot"],
        )

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
        payment = ProviderOrderPaymentOrder.objects.get(order__order_no=order_no)
        self.assertEqual(payment.status, ProviderOrderPaymentOrder.Status.PENDING_PAYMENT)
        self.assertEqual(payment.service_fee_amount, 35600)
        self.assertEqual(payment.transport_fee_amount, 1000)
        self.assertEqual(payment.payable_amount, 36600)
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
        payment.refresh_from_db()
        self.assertEqual(payment.status, ProviderOrderPaymentOrder.Status.PAID)
        self.assertEqual(payment.channel, ProviderOrderPaymentOrder.Channel.MOCK_WECHAT)
        self.assertTrue(payment.gateway_trade_no.startswith("MOCKPAY"))
        original_gateway_trade_no = payment.gateway_trade_no
        repeated_payment = self.client.post(
            f"/api/v1/provider-orders/{order_no}/simulate-payment/"
        )
        self.assertEqual(repeated_payment.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.gateway_trade_no, original_gateway_trade_no)
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
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_PAYMENT_SUCCESS,
                target_id=order_no,
            ).count(),
            1,
        )

    def test_payment_and_acceptance_task_transition_is_atomic(self):
        created = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = created.json()["data"]["order_no"]

        with patch(
            "taskcenter.services.register_provider_acceptance_timeout",
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
        payment = ProviderOrderPaymentOrder.objects.get(order=order)
        self.assertEqual(payment.status, ProviderOrderPaymentOrder.Status.PENDING_PAYMENT)
        self.assertEqual(payment.gateway_trade_no, "")
        self.assertEqual(payment_task.status, ScheduledTask.Status.PENDING)
        self.assertFalse(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ACCEPTANCE_TIMEOUT,
                business_key=order_no,
            ).exists()
        )

    def test_payment_rejects_gateway_amount_mismatch(self):
        created = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = created.json()["data"]["order_no"]
        with patch(
            "orders.payment_gateway.MockProviderOrderPaymentGateway.confirm_payment",
            return_value=PaymentResult(
                gateway_trade_no="MISMATCHED-AMOUNT",
                paid_amount=1,
                signature_verified=True,
            ),
        ):
            response = self.client.post(
                f"/api/v1/provider-orders/{order_no}/simulate-payment/"
            )

        self.assertEqual(response.status_code, 400)
        order = ProviderOrder.objects.get(order_no=order_no)
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_PAYMENT)
        self.assertIsNone(order.paid_at)
        self.assertEqual(
            order.payment_order.status,
            ProviderOrderPaymentOrder.Status.PENDING_PAYMENT,
        )

    def test_payment_rejects_unverified_gateway_result(self):
        created = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = created.json()["data"]["order_no"]
        order = ProviderOrder.objects.get(order_no=order_no)
        with patch(
            "orders.payment_gateway.MockProviderOrderPaymentGateway.confirm_payment",
            return_value=PaymentResult(
                gateway_trade_no="UNVERIFIED-RESULT",
                paid_amount=order.payable_amount,
                signature_verified=False,
            ),
        ):
            response = self.client.post(
                f"/api/v1/provider-orders/{order_no}/simulate-payment/"
            )

        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_PAYMENT)
        self.assertIsNone(order.paid_at)

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

    @patch(
        "orders.serializers.build_media_url",
        return_value="https://media.test/after-sales.webp",
    )
    def test_customer_can_submit_and_read_provider_order_after_sales(self, _build_url):
        order = self.create_paid_accepted_order()
        self.client.force_login(self.customer)
        evidence = MediaAsset.objects.create(
            owner=self.customer,
            scope=MediaAsset.Scope.PRIVATE,
            category=MediaAsset.Category.SUPPORT_ATTACHMENT,
            status=MediaAsset.Status.UPLOADED,
            object_key="private/support-attachments/order-after-sales.webp",
        )
        endpoint = f"/api/v1/provider-orders/{order.order_no}/after-sales/"

        excessive = self.client.post(
            endpoint,
            {
                "case_type": ProviderOrderAfterSalesCase.CaseType.REFUND,
                "requested_amount": order.payable_amount + 1,
                "reason": "服务内容与预约约定不一致，申请退款。",
                "evidence_asset_ids": [str(evidence.id)],
            },
            content_type="application/json",
        )
        self.assertEqual(excessive.status_code, 400)

        created = self.client.post(
            endpoint,
            {
                "case_type": ProviderOrderAfterSalesCase.CaseType.REFUND,
                "requested_amount": order.payable_amount,
                "reason": "服务内容与预约约定不一致，申请退款。",
                "evidence_asset_ids": [str(evidence.id)],
            },
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        case_no = created.json()["data"]["case_no"]
        self.assertEqual(created.json()["data"]["status"], "pending")
        self.assertEqual(
            created.json()["data"]["evidence_urls"],
            ["https://media.test/after-sales.webp"],
        )
        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.AFTER_SALES)
        conflicting_order = self.client.post(
            "/api/v1/provider-orders/",
            self.payload(),
            content_type="application/json",
        )
        self.assertEqual(conflicting_order.status_code, 400)
        self.assertIn("该时间段刚刚被预约", str(conflicting_order.json()))
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_AFTER_SALES_STARTED,
                target_id=order.order_no,
            ).exists()
        )

        repeated = self.client.post(
            endpoint,
            {
                "case_type": ProviderOrderAfterSalesCase.CaseType.OTHER,
                "requested_amount": order.payable_amount,
                "reason": "重复点击提交不应创建新的售后申请。",
            },
            content_type="application/json",
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()["data"]["case_no"], case_no)
        self.assertEqual(ProviderOrderAfterSalesCase.objects.filter(order=order).count(), 1)

        cases = self.client.get(endpoint)
        detail = self.client.get(f"/api/v1/provider-orders/{order.order_no}/")
        self.assertEqual(cases.status_code, 200)
        self.assertEqual(cases.json()["data"]["items"][0]["case_no"], case_no)
        self.assertEqual(detail.json()["data"]["after_sales"]["case_no"], case_no)

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
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_ACCEPTED,
                target_id=order_no,
            ).count(),
            1,
        )

        self.client.force_login(self.customer)
        customer_order = self.client.get(f"/api/v1/provider-orders/{order_no}/")
        self.assertEqual(
            customer_order.json()["data"]["status"],
            ProviderOrder.Status.PENDING_SERVICE,
        )

    def test_provider_rejects_paid_order_without_reason_and_starts_support_deadline(self):
        create = self.client.post(
            "/api/v1/provider-orders/", self.payload(), content_type="application/json"
        )
        order_no = create.json()["data"]["order_no"]
        self.client.post(f"/api/v1/provider-orders/{order_no}/simulate-payment/")
        self.client.force_login(self.provider_user)

        rejected = self.client.post(
            f"/api/v1/providers/me/orders/{order_no}/reject/",
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["data"]["status"], ProviderOrder.Status.PENDING_SUPPORT)
        self.assertEqual(rejected.json()["data"]["provider_rejection_reason"], "")
        self.assertIsNotNone(rejected.json()["data"]["provider_rejected_at"])
        self.assertEqual(rejected.json()["data"]["contact_phone_display"], "138****6688")
        order = ProviderOrder.objects.get(order_no=order_no)
        self.assertEqual(
            order.support_contact_deadline_at,
            order.provider_rejected_at + timedelta(minutes=15),
        )
        support_task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.PROVIDER_REJECTION_SUPPORT_TIMEOUT,
            business_key=order_no,
        )
        self.assertEqual(support_task.scheduled_at, order.support_contact_deadline_at)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                actor=self.provider_user,
                action="provider_order.provider_reject",
                target_id=order_no,
            ).exists()
        )

        repeated = self.client.post(
            f"/api/v1/providers/me/orders/{order_no}/reject/",
        )
        self.assertEqual(repeated.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.support_contact_deadline_at, support_task.scheduled_at)
        self.assertEqual(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_REJECTION_SUPPORT_TIMEOUT,
                business_key=order_no,
            ).count(),
            1,
        )
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
                target_id=order_no,
            ).count(),
            1,
        )

        cannot_accept = self.client.post(f"/api/v1/providers/me/orders/{order_no}/accept/")
        self.assertEqual(cannot_accept.status_code, 400)

        self.client.force_login(self.customer)
        customer_detail = self.client.get(f"/api/v1/provider-orders/{order_no}/")
        self.assertEqual(customer_detail.status_code, 200)
        self.assertEqual(customer_detail.json()["data"]["provider_rejection_reason"], "")

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
        self.assertSetEqual(
            set(
                UserNotification.objects.filter(
                    recipient=self.customer,
                    target_id=order.order_no,
                ).values_list("event_type", flat=True)
            ),
            {
                UserNotification.EventType.ORDER_PAYMENT_SUCCESS,
                UserNotification.EventType.ORDER_ACCEPTED,
                UserNotification.EventType.ORDER_DEPARTED,
                UserNotification.EventType.ORDER_STARTED,
                UserNotification.EventType.ORDER_COMPLETION_SUBMITTED,
            },
        )

        self.client.force_login(self.customer)
        category = self.service.category
        category.platform_commission_rate = Decimal("30.00")
        category.save(update_fields=("platform_commission_rate", "updated_at"))
        confirmed = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/confirm-completion/"
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["data"]["status"], ProviderOrder.Status.PENDING_REVIEW)
        self.assertIsNotNone(confirmed.json()["data"]["customer_confirmed_at"])
        settlement = ProviderOrderSettlement.objects.get(order=order)
        self.assertEqual(settlement.status, ProviderOrderSettlement.Status.RISK_FROZEN)
        self.assertEqual(settlement.paid_amount, 36600)
        self.assertEqual(settlement.platform_commission_rate, Decimal("20.00"))
        self.assertEqual(settlement.platform_commission_amount, 7120)
        self.assertEqual(settlement.provider_service_income_amount, 28480)
        self.assertEqual(settlement.provider_settlement_amount, 29480)
        self.assertEqual(
            confirmed.json()["data"]["settlement"]["settlement_no"],
            settlement.settlement_no,
        )
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
                business_key=order.order_no,
                status=ScheduledTask.Status.PENDING,
            ).exists()
        )
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

        review_image = MediaAsset.objects.create(
            owner=self.customer,
            scope=MediaAsset.Scope.PUBLIC,
            category=MediaAsset.Category.REVIEW_IMAGE,
            status=MediaAsset.Status.UPLOADED,
            object_key=f"public/review-images/{self.customer.public_id}/review.webp",
        )
        reviewed = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/review/",
            {
                "rating": 5,
                "content": "达人很细心，服务体验很好。",
                "image_ids": [str(review_image.id)],
                "is_anonymous": True,
            },
            content_type="application/json",
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.json()["data"]["status"], ProviderOrder.Status.COMPLETED)
        self.assertEqual(reviewed.json()["data"]["review"]["rating"], 5)
        self.assertEqual(reviewed.json()["data"]["review"]["customer_name"], "匿名用户")
        self.assertEqual(len(reviewed.json()["data"]["review"]["image_urls"]), 1)
        self.provider.refresh_from_db()
        self.assertEqual(self.provider.rating, Decimal("5.00"))
        self.assertEqual(self.provider.service_count, 1)

        repeated_review = self.client.post(
            f"/api/v1/provider-orders/{order.order_no}/review/",
            {"rating": 1, "content": "不会覆盖已有评价。"},
            content_type="application/json",
        )
        self.assertEqual(repeated_review.status_code, 200)
        self.assertEqual(repeated_review.json()["data"]["review"]["rating"], 5)

        my_reviews = self.client.get("/api/v1/users/me/provider-reviews/")
        self.assertEqual(my_reviews.status_code, 200)
        self.assertEqual(my_reviews.json()["data"]["pagination"]["total"], 1)
        self.assertEqual(my_reviews.json()["data"]["items"][0]["order_no"], order.order_no)
        self.assertTrue(my_reviews.json()["data"]["items"][0]["is_anonymous"])

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
        self.assertTrue(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
                target_id=order_no,
            ).exists()
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

    def test_order_creation_accepts_stale_location_when_auto_expiry_is_disabled(self):
        ProviderLiveLocation.objects.filter(pk=self.live_location.pk).update(
            received_at=timezone.now() - timedelta(days=7)
        )
        setting = ProviderOrderingSetting.current()
        setting.location_timeout_minutes = 0
        setting.save(update_fields=("location_timeout_minutes", "updated_at"))

        response = self.client.post(
            "/api/v1/provider-orders/preview/",
            self.payload(),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
