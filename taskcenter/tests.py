from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from backoffice.models import AdminAuditLog
from notifications.models import UserNotification
from orders.models import ProviderOrder
from providers.models import ProviderProfile, ProviderService, ServiceCategory

from .models import ScheduledTask
from .services import (
    process_due_tasks,
    register_provider_acceptance_timeout,
    register_provider_order_confirmation_timeout,
    register_provider_order_payment_expiry,
    synchronize_provider_order_tasks,
)
from .tasks import process_due_scheduled_tasks, synchronize_scheduled_tasks


class TaskCenterTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(phone="18800001001", password="test")
        provider_user = User.objects.create_user(
            phone="18800001002", password="test", nickname="任务测试达人"
        )
        self.provider = ProviderProfile.objects.create(
            user=provider_user,
            status=ProviderProfile.Status.APPROVED,
            service_city_code="130400",
            service_city_name="邯郸市",
        )
        category = ServiceCategory.objects.create(name="任务测试服务", slug="task-test")
        self.service = ProviderService.objects.create(
            provider=self.provider,
            category=category,
            billing_type=ProviderService.BillingType.HOURLY,
            price_amount=10000,
        )

    def make_order(
        self, *, order_no, status, payment_deadline, acceptance_deadline=None,
        completion_submitted_at=None, confirmation_deadline=None,
    ):
        starts_at = timezone.now() + timedelta(days=1)
        return ProviderOrder.objects.create(
            order_no=order_no,
            customer=self.customer,
            provider=self.provider,
            service=self.service,
            provider_name_snapshot="任务测试达人",
            service_name_snapshot="任务测试服务",
            billing_type_snapshot=ProviderOrder.BillingType.HOURLY,
            unit_price_amount=10000,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            duration_minutes=120,
            meeting_location_name="测试地点",
            meeting_address="测试地址1号",
            contact_name="测试用户",
            contact_gender=ProviderOrder.ContactGender.MR,
            contact_phone="18800001001",
            service_fee_amount=20000,
            payable_amount=20000,
            pricing_snapshot={"version": "test"},
            status=status,
            payment_expires_at=payment_deadline,
            paid_at=(timezone.now() - timedelta(minutes=40))
            if status == ProviderOrder.Status.PENDING_ACCEPTANCE
            else None,
            acceptance_expires_at=acceptance_deadline,
            completion_submitted_at=completion_submitted_at,
            confirmation_expires_at=confirmation_deadline,
        )

    def test_payment_expiry_task_closes_order_and_is_idempotent(self):
        now = timezone.now()
        order = self.make_order(
            order_no="DZYTASKPAY001",
            status=ProviderOrder.Status.PENDING_PAYMENT,
            payment_deadline=now - timedelta(seconds=1),
        )
        task, created = register_provider_order_payment_expiry(order)
        self.assertTrue(created)

        result = process_due_tasks(now=now)

        order.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(order.status, ProviderOrder.Status.CANCELLED)
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)
        self.assertEqual(task.result["action"], "cancelled")
        self.assertEqual(process_due_tasks(now=now)["claimed"], 0)

    def test_acceptance_timeout_moves_order_to_support(self):
        now = timezone.now()
        order = self.make_order(
            order_no="DZYTASKACCEPT001",
            status=ProviderOrder.Status.PENDING_ACCEPTANCE,
            payment_deadline=now - timedelta(minutes=30),
            acceptance_deadline=now - timedelta(seconds=1),
        )
        task, _ = register_provider_acceptance_timeout(order)

        result = process_due_tasks(now=now)

        order.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_SUPPORT)
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)
        self.assertEqual(task.result["action"], "moved_to_support")
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
                target_id=order.order_no,
            ).count(),
            1,
        )
        self.assertEqual(process_due_tasks(now=now)["claimed"], 0)
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_PENDING_SUPPORT,
                target_id=order.order_no,
            ).count(),
            1,
        )

    def test_confirmation_timeout_moves_order_to_pending_review(self):
        now = timezone.now()
        order = self.make_order(
            order_no="DZYTASKCONFIRM001",
            status=ProviderOrder.Status.PENDING_CONFIRMATION,
            payment_deadline=now - timedelta(days=1),
            completion_submitted_at=now - timedelta(days=3),
            confirmation_deadline=now - timedelta(seconds=1),
        )
        task, _ = register_provider_order_confirmation_timeout(order)

        result = process_due_tasks(now=now)

        order.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(order.status, ProviderOrder.Status.PENDING_REVIEW)
        self.assertEqual(order.auto_confirmed_at, now)
        self.assertIsNone(order.customer_confirmed_at)
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)
        self.assertEqual(task.result["action"], "auto_confirmed")
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_AUTO_CONFIRMED,
                target_id=order.order_no,
            ).count(),
            1,
        )
        self.assertEqual(process_due_tasks(now=now)["claimed"], 0)
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.customer,
                event_type=UserNotification.EventType.ORDER_AUTO_CONFIRMED,
                target_id=order.order_no,
            ).count(),
            1,
        )

    def test_compensation_creates_confirmation_deadline_and_task_for_legacy_order(self):
        now = timezone.now()
        completed_at = now - timedelta(days=1)
        order = self.make_order(
            order_no="DZYTASKCONFIRMLEGACY",
            status=ProviderOrder.Status.PENDING_CONFIRMATION,
            payment_deadline=now - timedelta(days=2),
            completion_submitted_at=completed_at,
        )

        result = synchronize_provider_order_tasks()

        order.refresh_from_db()
        task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_CONFIRMATION_TIMEOUT,
            business_key=order.order_no,
        )
        self.assertEqual(result["confirmation_created"], 1)
        self.assertEqual(order.confirmation_expires_at, completed_at + timedelta(days=3))
        self.assertEqual(task.scheduled_at, order.confirmation_expires_at)

    def test_compensation_sync_processes_only_a_bounded_missing_batch(self):
        now = timezone.now()
        for index in range(3):
            self.make_order(
                order_no=f"DZYTASKSYNC{index:03d}",
                status=ProviderOrder.Status.PENDING_PAYMENT,
                payment_deadline=now + timedelta(minutes=15),
            )

        first = synchronize_provider_order_tasks(batch_size=2)
        second = synchronize_provider_order_tasks(batch_size=2)
        third = synchronize_provider_order_tasks(batch_size=2)

        self.assertEqual(first["payment_created"], 2)
        self.assertEqual(second["payment_created"], 1)
        self.assertEqual(third["payment_created"], 0)
        self.assertEqual(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_PAYMENT_EXPIRY
            ).count(),
            3,
        )

    def test_periodic_processing_and_compensation_are_separate(self):
        with (
            patch("taskcenter.tasks.process_due_tasks", return_value={"claimed": 0}) as process,
            patch("taskcenter.tasks.synchronize_provider_order_tasks") as synchronize,
        ):
            self.assertEqual(process_due_scheduled_tasks(), {"claimed": 0})
            process.assert_called_once_with(limit=100)
            synchronize.assert_not_called()

        with (
            patch("taskcenter.tasks.process_due_tasks") as process,
            patch(
                "taskcenter.tasks.synchronize_provider_order_tasks",
                return_value={
                    "payment_created": 0,
                    "acceptance_created": 0,
                    "confirmation_created": 0,
                },
            ) as synchronize,
        ):
            self.assertEqual(
                synchronize_scheduled_tasks(),
                {
                    "payment_created": 0,
                    "acceptance_created": 0,
                    "confirmation_created": 0,
                },
            )
            synchronize.assert_called_once_with()
            process.assert_not_called()

    def test_failed_task_can_be_retried_from_admin_and_is_audited(self):
        now = timezone.now()
        task = ScheduledTask.objects.create(
            task_type="unsupported_test_task",
            business_type="provider_order",
            business_key="DZYUNKNOWN001",
            dedupe_key="unsupported_test_task:provider_order:DZYUNKNOWN001",
            scheduled_at=now - timedelta(minutes=2),
            available_at=now - timedelta(minutes=2),
            max_attempts=1,
        )
        result = process_due_tasks(now=now)
        task.refresh_from_db()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(task.status, ScheduledTask.Status.FAILED)
        self.assertIn("KeyError", task.last_error)

        admin = User.objects.create_superuser(
            phone="18800001999", password="test", nickname="任务管理员"
        )
        self.client.force_login(admin)
        listed = self.client.get("/api/v1/admin/tasks/?status=failed")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"]["summary"]["failed"], 1)

        retried = self.client.post(f"/api/v1/admin/tasks/{task.public_id}/retry/")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["data"]["status"], ScheduledTask.Status.PENDING)
        self.assertEqual(retried.json()["data"]["attempt_count"], 0)
        self.assertTrue(
            AdminAuditLog.objects.filter(
                action="system.task.retry",
                target_id=str(task.public_id),
            ).exists()
        )
