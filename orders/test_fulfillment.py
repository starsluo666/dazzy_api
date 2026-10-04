"""Offline fulfillment policy regressions; never call a payment channel."""
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from backoffice.models import AdminAuditLog, AdminRole, Organization, OrganizationMember, PlatformOperationSetting
from backoffice.serializers import PlatformOperationSettingSerializer
from taskcenter.models import ScheduledTask
from taskcenter.services import synchronize_provider_order_tasks
from .fulfillment import assess_timing, resolve_fulfillment_review
from .models import ProviderOrder
from .services import auto_confirm_provider_order, advance_provider_order_settlement
from .settlement_plans import sync_provider_settlement_plan
from . import tests as fixtures


LOCATION = {"longitude": "114.5060000", "latitude": "36.6200000", "accuracy_m": "12.50"}


@override_settings(DEBUG=True)
class FulfillmentControlsTests(TestCase):
    setUp = fixtures.ProviderOrderApiTests.setUp
    payload = fixtures.ProviderOrderApiTests.payload
    create_paid_accepted_order = fixtures.ProviderOrderApiTests.create_paid_accepted_order

    def post(self, order, action, data=None):
        return self.client.post(f"/api/v1/providers/me/orders/{order.order_no}/{action}/",
                                data=data or {}, content_type="application/json")

    def in_service_order(self):
        order = self.create_paid_accepted_order()
        now = timezone.now().replace(microsecond=0)
        order.starts_at = now - timedelta(minutes=120)
        order.ends_at = now
        order.service_started_at = order.starts_at
        order.departed_at = order.starts_at - timedelta(minutes=30)
        order.status = ProviderOrder.Status.IN_SERVICE
        order.save()
        return order, now

    def complete(self, order, now):
        with patch("orders.views.timezone.now", return_value=now):
            response = self.post(order, "complete", LOCATION)
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        return response

    def test_depart_requires_order_scoped_contact_and_explicit_confirmation(self):
        order = self.create_paid_accepted_order()
        self.assertEqual(self.post(order, "depart", {"contact_confirmed": True}).status_code, 400)
        response = self.post(order, "contact")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["data"]["provider_contact_initiated_at"])
        for data in ({}, {"contact_confirmed": False}):
            self.assertEqual(self.post(order, "depart", data).status_code, 400)
        self.assertEqual(self.post(order, "depart", {"contact_confirmed": True}).status_code, 200)
        order.refresh_from_db()
        self.assertIsNotNone(order.departure_contact_confirmed_at)
        confirmed = order.departure_contact_confirmed_at
        self.assertEqual(self.post(order, "depart").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.departure_contact_confirmed_at, confirmed)

    def test_contact_is_owner_only_and_unaccepted_order_is_not_allowed(self):
        order = self.create_paid_accepted_order()
        self.client.force_login(self.customer)
        self.assertIn(self.post(order, "contact").status_code, (403, 404))
        self.client.force_login(self.provider_user)
        order.status = ProviderOrder.Status.PENDING_ACCEPTANCE
        order.save()
        self.assertEqual(self.post(order, "contact").status_code, 400)
        order.refresh_from_db()
        self.assertIsNone(order.provider_contact_initiated_at)

    def test_completion_requires_valid_location_and_is_idempotent(self):
        order, now = self.in_service_order()
        for data in ({}, {**LOCATION, "longitude": "181"}, {**LOCATION, "latitude": "NaN"}, {**LOCATION, "accuracy_m": -1}):
            self.assertEqual(self.post(order, "complete", data).status_code, 400)
        self.complete(order, now)
        self.assertFalse(order.fulfillment_review_required)
        self.assertEqual(str(order.completion_longitude), LOCATION["longitude"])
        self.assertEqual(order.confirmation_expires_at, now + timedelta(days=3))
        before = (order.completion_submitted_at, order.confirmation_expires_at, order.completion_longitude)
        self.assertEqual(self.post(order, "complete", {**LOCATION, "longitude": "100"}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(before, (order.completion_submitted_at, order.confirmation_expires_at, order.completion_longitude))

    def test_exact_thirty_minute_boundary_and_one_second_outside(self):
        order, now = self.in_service_order()
        for stage in ("start", "completion"):
            expected = order.starts_at if stage == "start" else order.ends_at
            for seconds, held in ((-1800, False), (1800, False), (-1801, True), (1801, True)):
                with self.subTest(stage=stage, seconds=seconds):
                    order.fulfillment_policy = {}
                    order.fulfillment_issues = []
                    order.fulfillment_review_required = False
                    order.billing_type_snapshot = "per_session"
                    assess_timing(order, stage=stage, now=expected + timedelta(seconds=seconds))
                    self.assertEqual(order.fulfillment_review_required, held)

    def test_hourly_duration_and_per_session_are_distinct(self):
        order, now = self.in_service_order()
        order.service_started_at = now - timedelta(minutes=60)
        order.save(update_fields=("service_started_at",))
        self.complete(order, now)
        self.assertEqual([item["code"] for item in order.fulfillment_issues], ["duration_short"])
        order.fulfillment_issues = []
        order.fulfillment_review_required = False
        order.billing_type_snapshot = "per_session"
        assess_timing(order, stage="completion", now=now)
        self.assertFalse(order.fulfillment_review_required)

    def test_policy_is_snapshotted_and_no_historical_reclassification(self):
        order, now = self.in_service_order()
        setting = PlatformOperationSetting.current()
        setting.provider_order_early_tolerance_minutes = 10
        setting.provider_order_late_tolerance_minutes = 20
        setting.save()
        assess_timing(order, stage="start", now=order.starts_at)
        order.save()
        setting.provider_order_early_tolerance_minutes = 180
        setting.save()
        self.complete(order, now - timedelta(minutes=11))
        self.assertEqual(order.fulfillment_policy["early_minutes"], 10)
        self.assertTrue(order.fulfillment_review_required)
        # Existing completed records are untouched by migration/defaults or reads.
        order.fulfillment_policy = {}
        order.fulfillment_review_required = False
        order.status = ProviderOrder.Status.COMPLETED
        order.save()
        self.client.get(f"/api/v1/providers/me/orders/{order.order_no}/")
        order.refresh_from_db()
        self.assertFalse(order.fulfillment_review_required)
        self.assertEqual(order.fulfillment_policy, {})

    def test_hold_blocks_auto_confirmation_and_synchronizer_then_resume_remaining_window(self):
        order, now = self.in_service_order()
        self.complete(order, now + timedelta(minutes=31))
        self.assertTrue(order.fulfillment_review_required)
        self.assertIsNone(order.confirmation_expires_at)
        self.assertEqual(order.confirmation_remaining_seconds, 3 * 86400)
        later = now + timedelta(days=10)
        self.assertEqual(auto_confirm_provider_order(order.order_no, now=later)["state"], "fulfillment_held")
        synchronize_provider_order_tasks()
        self.assertFalse(ScheduledTask.objects.filter(business_key=order.order_no,
            task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT, status="pending").exists())
        with transaction.atomic():
            self.assertTrue(resolve_fulfillment_review(order, revision=1, reason="核实服务正常", actor=self.customer, now=later))
        order.refresh_from_db()
        self.assertEqual(order.confirmation_expires_at, later + timedelta(days=3))
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_CONFIRMATION)
        task = ScheduledTask.objects.get(business_key=order.order_no, task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT)
        self.assertEqual(task.status, "pending")
        self.assertEqual(auto_confirm_provider_order(order.order_no, now=later)["state"], "not_due")
        self.assertEqual(auto_confirm_provider_order(order.order_no, now=later + timedelta(days=3))["state"], "expired")

    def test_customer_may_confirm_but_hold_still_prevents_settlement_and_distribution(self):
        order, now = self.in_service_order()
        self.complete(order, now - timedelta(minutes=31))
        self.client.force_login(self.customer)
        response = self.client.post(f"/api/v1/provider-orders/{order.order_no}/confirm-completion/")
        self.assertEqual(response.status_code, 200)
        result = advance_provider_order_settlement(order_no=order.order_no, now=now + timedelta(days=10))
        self.assertEqual(result["state"], "fulfillment_held")
        plan = sync_provider_settlement_plan(order_no=order.order_no)
        self.assertIn("fulfillment_review", [item["code"] for item in plan.blockers])
        order.refresh_from_db()
        with transaction.atomic():
            resolve_fulfillment_review(order, revision=order.fulfillment_revision, reason="双方确认无误", actor=self.customer)
        result = advance_provider_order_settlement(order_no=order.order_no, now=now + timedelta(days=10))
        self.assertEqual(result["state"], "settled")

    def test_start_and_completion_raise_distinct_reviews_and_stale_review_is_rejected(self):
        order, now = self.in_service_order()
        assess_timing(order, stage="start", now=order.starts_at + timedelta(minutes=31))
        order.save()
        with transaction.atomic():
            resolve_fulfillment_review(order, revision=1, reason="用户同意延迟开始", actor=self.customer, now=now)
        self.complete(order, now + timedelta(minutes=31))
        self.assertEqual(order.fulfillment_revision, 2)
        with self.assertRaises(ValidationError):
            resolve_fulfillment_review(order, revision=1, reason="旧页面提交", actor=self.customer)
        self.assertTrue(order.fulfillment_review_required)
        with transaction.atomic():
            resolve_fulfillment_review(order, revision=2, reason="本次也已核实", actor=self.customer, now=now)
            deadline = order.confirmation_expires_at
            self.assertFalse(resolve_fulfillment_review(order, revision=2, reason="重复提交", actor=self.customer, now=now + timedelta(days=1)))
        self.assertEqual(order.confirmation_expires_at, deadline)
        self.assertEqual(len(order.fulfillment_reviews), 2)

    def test_admin_review_requires_permission_scope_reason_and_writes_audit(self):
        order, now = self.in_service_order()
        self.complete(order, now + timedelta(hours=1))
        staff = User.objects.create_user(phone="19900004567", nickname="测试客服")
        org = Organization.objects.create(name="测试运营", code="fulfillment-test", city_codes=["130400"], organization_type="city_agent")
        role = AdminRole.objects.create(organization=org, name="客服", code="cs", data_scope="city", permissions=["order.fulfillment.view"])
        OrganizationMember.objects.create(user=staff, organization=org, role=role)
        self.client.force_login(staff)
        url = f"/api/v1/admin/provider-orders/{order.order_no}/fulfillment-review/"
        payload = {"revision": order.fulfillment_revision, "reason": "已联系双方核实服务完成"}
        self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 403)
        role.permissions.append("order.fulfillment.review")
        role.save()
        org.city_codes = ["110100"]
        org.save()
        self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 404)
        org.city_codes = ["130400"]
        org.save()
        self.assertEqual(self.client.post(url, {**payload, "reason": " "}, content_type="application/json").status_code, 400)
        listing = self.client.get("/api/v1/admin/provider-orders/?anomaly=fulfillment_review")
        self.assertEqual(listing.json()["data"]["pagination"]["total"], 1)
        for _ in range(2):
            self.assertEqual(self.client.post(url, payload, content_type="application/json").status_code, 200)
        self.assertEqual(AdminAuditLog.objects.filter(action="order.fulfillment.review", target_id=order.order_no).count(), 1)
        listing = self.client.get("/api/v1/admin/provider-orders/?anomaly=fulfillment_resolved")
        self.assertEqual(listing.json()["data"]["pagination"]["total"], 1)

    def test_admin_tolerance_range_validation(self):
        for value in (-1, 181):
            serializer = PlatformOperationSettingSerializer(data={"provider_order_early_tolerance_minutes": value}, partial=True)
            self.assertFalse(serializer.is_valid())
        for value in (0, 30, 180):
            serializer = PlatformOperationSettingSerializer(data={"provider_order_early_tolerance_minutes": value, "provider_order_late_tolerance_minutes": value}, partial=True)
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_after_sales_review_does_not_lose_paused_confirmation_window(self):
        from taskcenter.services import reopen_provider_order_confirmation_timeout
        order, now = self.in_service_order()
        self.complete(order, now + timedelta(minutes=31))
        order.status = ProviderOrder.Status.AFTER_SALES
        order.save()
        later = now + timedelta(days=10)
        with transaction.atomic():
            resolve_fulfillment_review(order, revision=1, reason="履约已核实，售后继续处理", actor=self.customer, now=later)
        self.assertEqual(order.status, ProviderOrder.Status.AFTER_SALES)
        self.assertEqual(order.confirmation_expires_at, later + timedelta(days=3))
        self.assertFalse(ScheduledTask.objects.filter(business_key=order.order_no,
            task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT, status="pending").exists())
        order.status = ProviderOrder.Status.PENDING_CONFIRMATION
        order.save()
        with transaction.atomic():
            reopen_provider_order_confirmation_timeout(order)
        task = ScheduledTask.objects.get(business_key=order.order_no, task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT)
        self.assertEqual(task.scheduled_at, later + timedelta(days=3))

    def test_claimed_confirmation_worker_cannot_override_review_restart(self):
        from taskcenter.services import _finish_task, _fail_task, _claim_due_tasks, TaskExecutionOutcome
        from .fulfillment import suspend_fulfillment_tasks
        order, now = self.in_service_order()
        self.complete(order, now)
        task = ScheduledTask.objects.get(business_key=order.order_no, task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT)
        task.status = ScheduledTask.Status.RUNNING
        task.started_at = now
        task.attempt_count = 1
        task.save()
        old_lease = {"lease_started_at": task.started_at, "lease_attempt": task.attempt_count}
        order.fulfillment_review_required = True
        order.fulfillment_revision = 1
        order.confirmation_remaining_seconds = 3600
        order.confirmation_expires_at = None
        order.save()
        with transaction.atomic():
            suspend_fulfillment_tasks(order)
            resolve_fulfillment_review(order, revision=1, reason="已核实服务", actor=self.customer, now=now)
        # A previously claimed worker may finish after the review transaction.
        _finish_task(task.pk, TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED), now, **old_lease)
        task.refresh_from_db()
        self.assertEqual(task.status, ScheduledTask.Status.PENDING)
        self.assertEqual(task.scheduled_at, now + timedelta(hours=1))
        # Even if a new worker has already reclaimed the resumed task, neither
        # the old handler's result nor its exception may overwrite the new lease.
        later = now + timedelta(hours=1)
        claimed = _claim_due_tasks(now=later, limit=10, task_types=[task.task_type])
        self.assertEqual([item.pk for item in claimed], [task.pk])
        _finish_task(task.pk, TaskExecutionOutcome(status=ScheduledTask.Status.CANCELLED), now, **old_lease)
        _fail_task(task.pk, RuntimeError("stale worker"), now, **old_lease)
        task.refresh_from_db()
        self.assertEqual(task.status, ScheduledTask.Status.RUNNING)
        self.assertEqual(task.started_at, later)
        self.assertEqual(task.last_error, "")
        new_lease = {"lease_started_at": task.started_at, "lease_attempt": task.attempt_count}
        _finish_task(task.pk, TaskExecutionOutcome(status=ScheduledTask.Status.SUCCEEDED), later, **new_lease)
        task.refresh_from_db()
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)
