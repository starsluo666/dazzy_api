from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from activities import tests as activity_fixtures
from activities.huifu import create_activity_huifu_payment_session
from activities.models import Activity, ActivityPublishOrder
from activities.services import get_or_create_publish_order, simulate_publish_payment
from orders import tests as order_fixtures
from orders.models import ProviderOrder, ProviderOrderReview
from taskcenter.models import ScheduledTask
from backoffice.access import resolve_admin_access
from backoffice.models import AdminAuditLog
from backoffice.services import change_user_function_restrictions
from .account_closure import lock_active_user_for_business
from .models import User, UserFunctionRestriction as Restriction


@override_settings(DEBUG=True)
class RestrictedProviderOrderTests(TestCase):
    setUp = order_fixtures.ProviderOrderApiTests.setUp
    payload = order_fixtures.ProviderOrderApiTests.payload

    def restrict(self, function):
        return Restriction.objects.create(user=self.customer, function=function, reason="业务功能限制")

    def create_order(self):
        self.client.force_login(self.customer)
        return self.client.post("/api/v1/provider-orders/", self.payload(), content_type="application/json")

    def test_restriction_blocks_new_booking_even_if_applied_during_route_lookup(self):
        original = self.route_patcher
        original.stop()
        from locations.tencent import RouteResult
        from decimal import Decimal

        def route(*args, **kwargs):
            self.restrict("provider_order")
            return RouteResult(distance_km=Decimal("6.80"), duration_minutes=18)

        before = ScheduledTask.objects.count()
        with patch("orders.serializers.tencent_map.driving_route", side_effect=route):
            response = self.create_order()
        self.assertEqual(response.status_code, 403)
        self.assertIn("限制达人下单", str(response.json()))
        self.assertFalse(ProviderOrder.objects.filter(customer=self.customer).exists())
        self.assertEqual(ScheduledTask.objects.count(), before)

    def test_other_function_restrictions_do_not_block_booking_or_existing_cancellation(self):
        self.restrict("activity_publish")
        self.restrict("review")
        created = self.create_order()
        self.assertEqual(created.status_code, 201, created.content)
        order_no = created.json()["data"]["order_no"]
        self.restrict("provider_order")
        self.assertEqual(self.client.get(f"/api/v1/provider-orders/{order_no}/").status_code, 200)
        cancelled = self.client.post(f"/api/v1/provider-orders/{order_no}/cancel/", {}, content_type="application/json")
        self.assertEqual(cancelled.status_code, 200, cancelled.content)

    def test_review_restriction_blocks_submission_without_completing_order(self):
        created = self.create_order()
        self.assertEqual(created.status_code, 201, created.content)
        order_no = created.json()["data"]["order_no"]
        ProviderOrder.objects.filter(order_no=order_no).update(
            status=ProviderOrder.Status.PENDING_REVIEW,
            review_expires_at=timezone.now() + timedelta(days=2),
        )
        restriction = self.restrict("review")
        url = f"/api/v1/provider-orders/{order_no}/review/"
        payload = {"rating": 5, "content": "本次服务非常满意，下次再约"}
        response = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProviderOrderReview.objects.filter(order__order_no=order_no).exists())
        self.assertEqual(ProviderOrder.objects.get(order_no=order_no).status, ProviderOrder.Status.PENDING_REVIEW)
        restriction.is_active = False
        restriction.save(update_fields=("is_active",))
        self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 200)


@override_settings(DEBUG=True)
class RestrictedActivityTests(TestCase):
    setUp = activity_fixtures.ActivityModelTests.setUp
    build_activity = activity_fixtures.ActivityModelTests.build_activity
    activity_create_payload = activity_fixtures.ActivityModelTests.activity_create_payload

    def test_restriction_blocks_drafts_and_publish_order_but_not_viewing(self):
        self.client.force_login(self.organizer)
        restriction = Restriction.objects.create(
            user=self.organizer, function="activity_publish", reason="活动发布核查",
        )
        response = self.client.post("/api/v1/activities/", self.activity_create_payload(), content_type="application/json")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Activity.objects.filter(organizer=self.organizer).exists())
        restriction.is_active = False
        restriction.save(update_fields=("is_active",))
        activity = self.build_activity(status=Activity.Status.DRAFT)
        activity.save()
        order = get_or_create_publish_order(activity_id=activity.pk, user=self.organizer)
        restriction.is_active = True
        restriction.save(update_fields=("is_active",))
        with self.assertRaises(PermissionDenied):
            get_or_create_publish_order(activity_id=activity.pk, user=self.organizer)
        with self.assertRaises(PermissionDenied):
            simulate_publish_payment(activity_id=activity.pk, user=self.organizer)
        order.refresh_from_db()
        self.assertEqual(order.status, ActivityPublishOrder.Status.PENDING_PAYMENT)
        self.assertEqual(self.client.get("/api/v1/activities/").status_code, 200)

    @patch("activities.huifu.get_huifu_payment_gateway")
    @patch("config.payment_capabilities.ensure_activity_real_payment_available")
    def test_existing_publish_order_cannot_bypass_restriction_via_real_payment(self, _available, gateway):
        activity = self.build_activity(status=Activity.Status.DRAFT)
        activity.save()
        get_or_create_publish_order(activity_id=activity.pk, user=self.organizer)
        Restriction.objects.create(user=self.organizer, function="activity_publish", reason="发布限制")
        with self.assertRaises(PermissionDenied):
            create_activity_huifu_payment_session(
                payment_kind="activity_publish", activity_id=activity.pk,
                user_id=self.organizer.pk, payment_scene="official_account", sub_openid="fake",
            )
        gateway.return_value.create_payment.assert_not_called()


class FunctionRestrictionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL row locks")
        self.user = User.objects.create_user(phone="13800123450")
        self.admin = User.objects.create_superuser(phone="13800123451", password="test-password")
        self.access = resolve_admin_access(self.admin)
        self.request = SimpleNamespace(headers={}, META={})

    def change(self, function):
        return change_user_function_restrictions(
            public_id=self.user.public_id, action="restrict", functions=[function],
            reason="并发功能限制测试", actor=self.admin, access=self.access, request=self.request,
        )

    def test_independent_concurrent_restrictions_are_both_preserved(self):
        barrier = Barrier(2)

        def apply(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                self.change(function)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(apply, ["provider_order", "review"]))
        self.assertEqual(
            set(Restriction.objects.filter(user=self.user, is_active=True).values_list("function", flat=True)),
            {"provider_order", "review"},
        )
        self.assertEqual(AdminAuditLog.objects.filter(action="user.function.restrict").count(), 2)

    def test_business_waits_for_restriction_commit_and_cannot_use_stale_user(self):
        restricted, release, attempting = Event(), Event(), Event()

        def writer():
            close_old_connections()
            try:
                with transaction.atomic():
                    self.change("provider_order")
                    restricted.set()
                    if not release.wait(10):
                        raise AssertionError("restriction commit not released")
            finally:
                close_old_connections()

        def reader():
            close_old_connections()
            try:
                with transaction.atomic():
                    attempting.set()
                    try:
                        lock_active_user_for_business(self.user, function="provider_order")
                    except PermissionDenied:
                        return "denied"
                    return "allowed"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(writer)
            try:
                self.assertTrue(restricted.wait(10))
                read = pool.submit(reader)
                self.assertTrue(attempting.wait(10))
                with self.assertRaises(TimeoutError):
                    read.result(timeout=0.25)
            finally:
                release.set()
            write.result(timeout=10)
            self.assertEqual(read.result(timeout=10), "denied")
