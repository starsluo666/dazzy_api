from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from backoffice.models import PlatformOperationSetting
from providers.models import ProviderProfile
from taskcenter.models import ScheduledTask
from wallets.models import UserWallet
from wallets.test_consumption_discount import credit_lot

from . import tests as fixtures
from .models import ProviderOrder
from .renewals import create_renewal
from .services import apply_provider_order_payment_success, ensure_slot_available


@override_settings(DEBUG=True)
class ProviderOrderRenewalTests(TestCase):
    def setUp(self):
        fixtures.ProviderOrderApiTests.setUp(self)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("No live channel calls"))
        guard.start()
        self.addCleanup(guard.stop)
        self.now = timezone.make_aware(datetime(2026, 11, 21, 14))
        self.source = self.make_order("ROOT", self.now - timedelta(hours=1), ProviderOrder.Status.IN_SERVICE)
        self.source.service_started_at = self.source.starts_at
        self.source.save(update_fields=("service_started_at",))
        self.url = f"/api/v1/provider-orders/{self.source.order_no}/renewal/"

    def make_order(self, order_no, starts_at, status, *, customer=None):
        return ProviderOrder.objects.create(
            order_no=order_no, customer=customer or self.customer, provider=self.provider, service=self.service,
            provider_name_snapshot="晓晓", service_name_snapshot="陪伴",
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY, unit_price_amount=12000,
            service_fee_amount=24000, transport_fee_amount=1000, payable_amount=25000,
            starts_at=starts_at, ends_at=starts_at + timedelta(hours=2), duration_minutes=120,
            meeting_address=self.address.address, contact_name=self.address.contact_name,
            contact_phone=self.address.contact_phone, status=status,
            payment_expires_at=starts_at - timedelta(hours=1), paid_at=starts_at - timedelta(hours=2),
        )

    def request_renewal(self, duration=120, amount=24000):
        return self.client.post(self.url, {"duration_minutes": duration, "payable_amount": amount})

    def child(self):
        response = self.request_renewal()
        self.assertEqual(response.status_code, 201, response.json())
        return self.source.renewals.get()

    def pay(self, order, *, now=None, huifu_trade_type=None):
        return apply_provider_order_payment_success(
            order_no=order.order_no, customer_id=self.customer.pk, channel="wechat_h5",
            gateway_trade_no=f"TEST-{order.pk}", paid_amount=order.payable_amount,
            signature_verified=True, now=now, huifu_trade_type=huifu_trade_type,
        )

    def options(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()["data"]["options"]

    def test_quote_caps_at_17_and_reserves_buffer(self):
        self.make_order("NEXT", self.now + timedelta(hours=4), ProviderOrder.Status.PENDING_SERVICE)
        with patch("django.utils.timezone.now", return_value=self.now):
            options = self.options()
            self.assertEqual([slot["duration_minutes"] for slot in options], [30, 60, 90, 120])
            self.assertEqual(self.request_renewal(150, 30000).status_code, 400)
            self.assertEqual(self.request_renewal(120, 23000).status_code, 400)
            order = self.child()
            self.assertEqual(order.starts_at, self.source.ends_at)
            self.assertEqual(order.ends_at, self.now + timedelta(hours=3))
            self.assertEqual(order.renewal_travel_until, self.now + timedelta(hours=4))
            self.assertEqual((order.service_fee_amount, order.transport_fee_amount), (24000, 0))
            self.assertEqual(order.meeting_address, self.source.meeting_address)
            self.assertEqual(self.request_renewal().status_code, 200)
            with self.assertRaises(ValidationError):
                ensure_slot_available(self.provider, order.ends_at, order.renewal_travel_until)
        with patch("django.utils.timezone.now", return_value=order.payment_expires_at):
            ensure_slot_available(self.provider, order.ends_at, order.renewal_travel_until)

    def test_config_changes_cutoff_and_non_owner_is_rejected(self):
        self.make_order("NEXT", self.now + timedelta(hours=4), ProviderOrder.Status.PENDING_SERVICE)
        setting = PlatformOperationSetting.current()
        setting.provider_order_renewal_travel_minutes = 90
        setting.save()
        with patch("django.utils.timezone.now", return_value=self.now):
            self.assertEqual(self.options()[-1]["duration_minutes"], 90)
            self.client.force_login(self.provider_user)
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.request_renewal().status_code, 404)

    def test_renewal_respects_service_and_city_booking_rules(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            self.assertTrue(self.options())
            self.service.is_active = False
            self.service.save(update_fields=("is_active",))
            self.assertEqual(self.options(), [])
            self.assertEqual(self.request_renewal().status_code, 400)
            self.service.is_active = True
            self.service.save(update_fields=("is_active",))
            setting = PlatformOperationSetting.current()
            setting.discovery_cities = []
            setting.save(update_fields=("discovery_cities",))
            self.assertEqual(self.options(), [])
            self.assertEqual(self.request_renewal().status_code, 400)

    def test_wallet_discount_and_checkout_only_charge_service(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            credit_lot(self.customer, 30000, 9500)
            option = next(slot for slot in self.options() if slot["duration_minutes"] == 120)
            self.assertEqual(option["payable_amount"], 22800)
            self.assertEqual(self.request_renewal(amount=22800).status_code, 201)
            order = self.source.renewals.get()
            response = self.client.post(
                f"/api/v1/provider-orders/{order.order_no}/payment-session/",
                {"payment_scene": "official_account"}, content_type="application/json",
            )
            self.assertEqual(response.status_code, 201, response.json())
            self.assertEqual(response.json()["data"]["invoke_type"], "BALANCE")
            order.refresh_from_db()
            self.assertEqual((order.status, order.discount_amount, order.transport_fee_amount), ("pending_service", 1200, 0))
            wallet = UserWallet.objects.get(user=self.customer)
            self.assertEqual((wallet.available_balance, wallet.frozen_balance), (7200, 0))

    def test_per_session_renewal_respects_the_original_duration(self):
        self.source.billing_type_snapshot = ProviderOrder.BillingType.PER_SESSION
        self.source.save(update_fields=("billing_type_snapshot",))
        with patch("django.utils.timezone.now", return_value=self.now):
            self.assertEqual([(slot["duration_minutes"], slot["payable_amount"]) for slot in self.options()], [(120, 12000)])
            self.assertEqual(self.request_renewal(90, 12000).status_code, 400)
            self.assertEqual(self.request_renewal(120, 12000).status_code, 201)

    def test_payment_goes_to_service_and_skips_departure(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            order = self.child()
            self.assertTrue(self.pay(order)[2])
            self.assertFalse(self.pay(order)[2])
            order.refresh_from_db()
            self.assertEqual(order.status, ProviderOrder.Status.PENDING_SERVICE)
            self.assertIsNone(order.acceptance_expires_at)
            tasks = ScheduledTask.objects.filter(business_key=order.order_no)
            self.assertTrue(tasks.filter(task_type=ScheduledTask.Type.PROVIDER_START_TIMEOUT).exists())
            self.assertFalse(tasks.filter(task_type=ScheduledTask.Type.PROVIDER_DEPARTURE_TIMEOUT).exists())
            detail = self.client.get(f"/api/v1/provider-orders/{order.order_no}/").json()["data"]
            self.assertEqual(detail["renewal_of_order_no"], self.source.order_no)
            refund_quote = self.client.get(f"/api/v1/provider-orders/{order.order_no}/cancellation/").json()["data"]
            self.assertEqual(refund_quote["refund_amount"], order.payable_amount)
            self.client.force_login(self.provider_user)
            base = f"/api/v1/providers/me/orders/{order.order_no}"
            self.assertEqual(self.client.post(f"{base}/depart/").status_code, 400)
            self.assertEqual(self.client.post(f"{base}/start/").status_code, 400)
        with patch("django.utils.timezone.now", return_value=order.starts_at):
            self.assertEqual(self.client.post(f"{base}/start/").status_code, 400)
            ProviderOrder.objects.filter(pk=self.source.pk).update(status=ProviderOrder.Status.PENDING_CONFIRMATION)
            response = self.client.post(f"{base}/start/")
            self.assertEqual(response.status_code, 200, response.json())
            order.refresh_from_db()
            self.assertEqual(order.status, ProviderOrder.Status.IN_SERVICE)
            self.assertIsNone(order.departed_at)
            self.assertIsNone(order.arrival_photo_id)

    def test_stale_huifu_success_is_refunded(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            order = self.child()
        with patch("django.utils.timezone.now", return_value=order.payment_expires_at + timedelta(seconds=1)):
            paid_at = order.payment_expires_at - timedelta(seconds=1)
            self.pay(order, now=paid_at, huifu_trade_type="T_JSAPI")
        order.refresh_from_db()
        self.assertEqual(order.status, ProviderOrder.Status.CANCELLED)
        self.assertEqual(order.refund_orders.get().refund_amount, order.payable_amount)

    def test_late_next_booking_payment_cannot_overlap_a_renewal(self):
        next_order = self.make_order("NEXT", self.now + timedelta(hours=3, minutes=30), ProviderOrder.Status.PENDING_PAYMENT)
        next_order.payment_expires_at = self.now - timedelta(minutes=1)
        next_order.paid_at = None
        next_order.save(update_fields=("payment_expires_at", "paid_at"))
        with patch("django.utils.timezone.now", return_value=self.now):
            self.child()
            paid_at = next_order.payment_expires_at - timedelta(seconds=1)
            self.pay(next_order, now=paid_at, huifu_trade_type="T_JSAPI")
        next_order.refresh_from_db()
        self.assertEqual(next_order.status, ProviderOrder.Status.CANCELLED)
        self.assertEqual(next_order.refund_orders.get().refund_amount, next_order.payable_amount)

    def test_second_renewal_can_use_the_same_trip_buffer(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            order = self.child()
            self.pay(order)
        order.status = ProviderOrder.Status.IN_SERVICE
        order.service_started_at = order.starts_at
        order.save(update_fields=("status", "service_started_at"))
        self.url = f"/api/v1/provider-orders/{order.order_no}/renewal/"
        with patch("django.utils.timezone.now", return_value=order.starts_at + timedelta(minutes=15)):
            self.assertEqual([slot["duration_minutes"] for slot in self.options()], [30, 60])

    def test_paid_renewal_buffer_stays_reserved_after_confirmation(self):
        with patch("django.utils.timezone.now", return_value=self.now):
            order = self.child()
            self.pay(order)
            ProviderOrder.objects.filter(pk=order.pk).update(status=ProviderOrder.Status.COMPLETED)
            with self.assertRaises(ValidationError):
                ensure_slot_available(self.provider, order.ends_at, order.renewal_travel_until)
            ensure_slot_available(self.provider, order.renewal_travel_until, order.renewal_travel_until + timedelta(hours=2))


@skipUnless(connection.vendor == "postgresql", "Provider schedule row locks require PostgreSQL")
class ProviderOrderRenewalConcurrencyTests(TransactionTestCase):
    setUp = ProviderOrderRenewalTests.setUp
    make_order = ProviderOrderRenewalTests.make_order

    def test_renewal_and_normal_booking_cannot_both_reserve_the_slot(self):
        start = self.now + timedelta(hours=3, minutes=30)
        ready = Barrier(2)
        next_customer = User.objects.create_user(phone="13800000103", password="test")

        def reserve_renewal():
            create_renewal(self.source, user=self.customer, duration=120, expected_payable=24000, order_no="RENEW")

        def reserve_normal():
            with transaction.atomic():
                provider = ProviderProfile.objects.select_for_update().get(pk=self.provider.pk)
                ensure_slot_available(provider, start, start + timedelta(hours=2))
                self.make_order("NEXT", start, ProviderOrder.Status.PENDING_SERVICE, customer=next_customer)

        def race(action):
            try:
                ready.wait(timeout=15)
                action()
                return True
            except ValidationError:
                return False
            finally:
                connections.close_all()

        with patch("django.utils.timezone.now", return_value=self.now), ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(race, (reserve_renewal, reserve_normal)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(ProviderOrder.objects.filter(order_no__in=("RENEW", "NEXT")).count(), 1)
