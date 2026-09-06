from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from activities.models import (
    Activity,
    ActivityCategory,
    ActivityParticipation,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivityPublishOrder,
    ActivitySettlement,
)
from activities.services import (
    create_activity_participation_refund,
    get_or_create_participation_order,
)
from backoffice.models import AdminAuditLog
from notifications.models import UserNotification
from orders.models import (
    ProviderOrder,
    ProviderOrderPaymentOrder,
    ProviderOrderRefundOrder,
    ProviderOrderSettlement,
)
from orders.services import create_provider_order_payment_order, create_provider_order_refund
from providers.models import ProviderProfile, ProviderService, ServiceCategory

from .models import ScheduledTask
from .services import (
    process_due_tasks,
    register_provider_acceptance_timeout,
    register_provider_order_confirmation_timeout,
    register_provider_order_payment_expiry,
    register_activity_lifecycle_tasks,
    synchronize_activity_tasks,
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
        now = timezone.now()
        starts_at = now + timedelta(days=1)
        is_paid = status not in (
            ProviderOrder.Status.PENDING_PAYMENT,
            ProviderOrder.Status.CANCELLED,
        )
        order = ProviderOrder.objects.create(
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
            paid_at=now - timedelta(minutes=40) if is_paid else None,
            acceptance_expires_at=acceptance_deadline,
            completion_submitted_at=completion_submitted_at,
            confirmation_expires_at=confirmation_deadline,
        )
        payment, _ = create_provider_order_payment_order(order)
        if is_paid:
            payment.status = ProviderOrderPaymentOrder.Status.PAID
            payment.channel = ProviderOrderPaymentOrder.Channel.MOCK_WECHAT
            payment.gateway_trade_no = f"MOCK-{order_no}"
            payment.paid_at = order.paid_at
            payment.save(
                update_fields=(
                    "status", "channel", "gateway_trade_no", "paid_at", "updated_at",
                )
            )
        return order

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
        order.payment_order.refresh_from_db()
        self.assertEqual(
            order.payment_order.status, ProviderOrderPaymentOrder.Status.CLOSED
        )
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)
        self.assertEqual(task.result["action"], "cancelled")
        self.assertEqual(process_due_tasks(now=now)["claimed"], 0)

    def test_stale_provider_refund_processing_is_recovered_idempotently(self):
        order = self.make_order(
            order_no="DZYTASKREFUND001",
            status=ProviderOrder.Status.AFTER_SALES,
            payment_deadline=timezone.now() - timedelta(hours=1),
        )
        refund, _ = create_provider_order_refund(
            order_no=order.order_no,
            amount=order.payable_amount,
            source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
            source_reference="task-recovery",
            idempotency_key="task-recovery-provider-refund",
            reason="测试退款恢复",
        )
        stale_at = timezone.now() - timedelta(minutes=6)
        ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(
            status=ProviderOrderRefundOrder.Status.PROCESSING,
            updated_at=stale_at,
        )

        result = process_due_tasks(
            task_types=[ScheduledTask.Type.PROVIDER_ORDER_REFUND],
            now=timezone.now(),
        )

        self.assertEqual(result["succeeded"], 1)
        refund.refresh_from_db()
        self.assertEqual(refund.status, ProviderOrderRefundOrder.Status.SUCCEEDED)
        self.assertTrue(refund.gateway_refund_no.startswith("MOCKREF"))

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
        settlement = ProviderOrderSettlement.objects.get(order=order)
        self.assertEqual(settlement.status, ProviderOrderSettlement.Status.RISK_FROZEN)
        self.assertEqual(settlement.platform_commission_amount, 4000)
        self.assertEqual(settlement.provider_settlement_amount, 16000)
        settlement_task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_SETTLEMENT,
            business_key=order.order_no,
        )
        self.assertEqual(settlement_task.status, ScheduledTask.Status.PENDING)
        self.assertEqual(settlement_task.available_at, settlement.freeze_until)
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

        settlement_result = process_due_tasks(now=settlement.freeze_until)
        settlement.refresh_from_db()
        settlement_task.refresh_from_db()
        self.assertEqual(settlement_result["succeeded"], 1)
        self.assertEqual(settlement.status, ProviderOrderSettlement.Status.SETTLED)
        self.assertEqual(settlement.settled_at, settlement.freeze_until)
        self.assertEqual(settlement_task.status, ScheduledTask.Status.SUCCEEDED)
        self.assertEqual(settlement_task.result["state"], "settled")
        self.assertEqual(
            UserNotification.objects.filter(
                recipient=self.provider.user,
                event_type=UserNotification.EventType.PROVIDER_ORDER_SETTLED,
                target_id=settlement.settlement_no,
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

    def test_compensation_backfills_missing_provider_refund_task(self):
        order = self.make_order(
            order_no="DZYTASKREFUNDSYNC001",
            status=ProviderOrder.Status.AFTER_SALES,
            payment_deadline=timezone.now() - timedelta(hours=1),
        )
        refund = ProviderOrderRefundOrder.objects.create(
            idempotency_key="task-sync-provider-refund",
            order=order,
            payment_order=order.payment_order,
            beneficiary=self.customer,
            source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
            source_reference="task-sync",
            service_fee_refund_amount=1000,
            transport_fee_refund_amount=0,
            other_fee_refund_amount=0,
            refund_amount=1000,
            allocation_snapshot={"version": "test"},
            reason="测试补建退款任务",
        )

        first = synchronize_provider_order_tasks()
        second = synchronize_provider_order_tasks()

        self.assertEqual(first["refund_task_created"], 1)
        self.assertEqual(second["refund_task_created"], 0)
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND,
                business_key=refund.refund_no,
            ).exists()
        )

    def test_periodic_processing_and_compensation_are_separate(self):
        with (
            patch("taskcenter.tasks.process_due_tasks", return_value={"claimed": 0}) as process,
            patch("taskcenter.tasks.synchronize_business_tasks") as synchronize,
        ):
            self.assertEqual(process_due_scheduled_tasks(), {"claimed": 0})
            process.assert_called_once_with(limit=100)
            synchronize.assert_not_called()

        with (
            patch("taskcenter.tasks.process_due_tasks") as process,
            patch(
                "taskcenter.tasks.synchronize_business_tasks",
                return_value={"provider_orders": {}, "activities": {}},
            ) as synchronize,
        ):
            self.assertEqual(
                synchronize_scheduled_tasks(),
                {"provider_orders": {}, "activities": {}},
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

    def test_provider_refund_notification_failure_does_not_rollback_refund(self):
        order = self.make_order(
            order_no="DZYTASKREFUNDNOTIFYFAIL",
            status=ProviderOrder.Status.AFTER_SALES,
            payment_deadline=timezone.now() - timedelta(hours=1),
        )
        refund, _ = create_provider_order_refund(
            order_no=order.order_no,
            amount=order.payable_amount,
            source_type=ProviderOrderRefundOrder.SourceType.SYSTEM,
            source_reference="notification-failure-test",
            idempotency_key="provider-refund-notification-failure",
            reason="验证通知失败不回滚退款",
        )

        with patch(
            "orders.services.create_order_notification",
            side_effect=RuntimeError("通知存储暂时不可用"),
        ), self.captureOnCommitCallbacks(execute=True):
            result = process_due_tasks(
                task_types=[ScheduledTask.Type.PROVIDER_ORDER_REFUND]
            )

        refund.refresh_from_db()
        order.refresh_from_db()
        order.payment_order.refresh_from_db()
        task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.PROVIDER_ORDER_REFUND,
            business_key=refund.refund_no,
        )
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(refund.status, ProviderOrderRefundOrder.Status.SUCCEEDED)
        self.assertEqual(order.status, ProviderOrder.Status.REFUNDED)
        self.assertEqual(
            order.payment_order.status, ProviderOrderPaymentOrder.Status.REFUNDED
        )
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)


class ActivityTaskCenterTests(TestCase):
    def setUp(self):
        self.organizer = User.objects.create_user(
            phone="18800002001",
            password="test",
            nickname="任务测试发起人",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.participant = User.objects.create_user(
            phone="18800002002",
            password="test",
            nickname="任务测试参与人",
            verification_status=User.VerificationStatus.VERIFIED,
        )
        self.category = ActivityCategory.objects.create(
            name="活动任务测试",
            slug="activity-task-test",
        )

    def make_activity(self, **overrides):
        starts_at = timezone.now() + timedelta(days=2)
        values = {
            "organizer": self.organizer,
            "category": self.category,
            "title": "活动任务中心测试局",
            "starts_at": starts_at,
            "ends_at": starts_at + timedelta(hours=2),
            "formation_deadline": starts_at - timedelta(hours=12),
            "meeting_place_name": "任务测试场馆",
            "meeting_address": "邯郸市任务测试地址",
            "city_code": "130400",
            "city_name": "邯郸市",
            "source_longitude": Decimal("114.4921000"),
            "source_latitude": Decimal("36.6123000"),
            "meeting_point": Point(114.485900, 36.610800, srid=4326),
            "capacity": 6,
            "min_participants": 2,
            "description": "活动任务中心测试",
            "participation_rules": "准时到场",
            "aa_principal_amount": 4800,
            "refund_template_version": "standard-v1",
            "refund_rule_snapshot": {"version": "standard-v1"},
            "status": Activity.Status.RECRUITING,
        }
        values.update(overrides)
        return Activity.objects.create(**values)

    def add_paid_participant(self, activity, *, now):
        participation = ActivityParticipation.objects.create(
            activity=activity,
            user=self.participant,
            status=ActivityParticipation.Status.ACTIVE,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            refund_rule_snapshot={"version": "standard-v1"},
            joined_at=now - timedelta(days=1),
        )
        ActivityParticipationPaymentOrder.objects.create(
            participation=participation,
            payer=self.participant,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            channel=ActivityParticipationPaymentOrder.Channel.MOCK_WECHAT,
            status=ActivityParticipationPaymentOrder.Status.PAID,
            expires_at=now - timedelta(hours=20),
            paid_at=now - timedelta(hours=21),
        )
        return participation

    def test_participation_payment_expiry_runs_through_task_center(self):
        activity = self.make_activity()
        participation, payment, _ = get_or_create_participation_order(
            activity_id=activity.pk,
            user=self.participant,
            channel=ActivityParticipationPaymentOrder.Channel.MOCK_WECHAT,
        )
        task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_PAYMENT_EXPIRY,
            business_key=payment.order_no,
        )

        result = process_due_tasks(now=payment.expires_at)

        payment.refresh_from_db()
        participation.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(payment.status, ActivityParticipationPaymentOrder.Status.CLOSED)
        self.assertEqual(participation.status, ActivityParticipation.Status.EXPIRED)
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)
        self.assertEqual(task.result["state"], "expired")

    def test_activity_refund_failure_is_recorded_and_retried(self):
        now = timezone.now()
        activity = self.make_activity()
        participation = self.add_paid_participant(activity, now=now)
        payment = participation.payment_orders.get()
        refund, created = create_activity_participation_refund(
            participation=participation,
            payment_order=payment,
            refund_type=(
                ActivityParticipationRefundOrder.RefundType.PARTICIPANT_CANCELLATION
            ),
            idempotency_key="task-activity-refund",
            principal_refund_amount=payment.aa_principal_amount,
            service_fee_refund_amount=payment.platform_service_fee_amount,
            reason="测试活动退款失败恢复",
        )
        self.assertTrue(created)
        self.assertEqual(refund.status, ActivityParticipationRefundOrder.Status.PENDING)

        with patch(
            "activities.payment_gateway.MockActivityPaymentGateway.refund",
            side_effect=RuntimeError("模拟活动退款渠道超时"),
        ):
            first = process_due_tasks(
                task_types=[ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND],
                now=timezone.now(),
            )
        self.assertEqual(first["retried"], 1)
        refund.refresh_from_db()
        self.assertEqual(refund.status, ActivityParticipationRefundOrder.Status.FAILED)
        self.assertIn("渠道超时", refund.failure_reason)
        payment.refresh_from_db()
        self.assertEqual(payment.status, ActivityParticipationPaymentOrder.Status.PAID)

        task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
            business_key=refund.refund_no,
        )
        second = process_due_tasks(
            task_types=[ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND],
            now=task.available_at,
        )
        self.assertEqual(second["succeeded"], 1)
        refund.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(refund.status, ActivityParticipationRefundOrder.Status.SUCCEEDED)
        self.assertEqual(payment.status, ActivityParticipationPaymentOrder.Status.REFUNDED)

    def test_activity_refund_notification_failure_does_not_rollback_refund(self):
        activity = self.make_activity()
        participation = self.add_paid_participant(activity, now=timezone.now())
        payment = participation.payment_orders.get()
        refund, _ = create_activity_participation_refund(
            participation=participation,
            payment_order=payment,
            refund_type=ActivityParticipationRefundOrder.RefundType.AFTER_SALES,
            idempotency_key="activity-refund-notification-failure",
            principal_refund_amount=payment.aa_principal_amount,
            service_fee_refund_amount=payment.platform_service_fee_amount,
            reason="验证通知失败不回滚退款",
        )

        with patch(
            "activities.services.create_activity_notification",
            side_effect=RuntimeError("通知存储暂时不可用"),
        ), self.captureOnCommitCallbacks(execute=True):
            result = process_due_tasks(
                task_types=[ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND]
            )

        refund.refresh_from_db()
        payment.refresh_from_db()
        task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
            business_key=refund.refund_no,
        )
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(
            refund.status, ActivityParticipationRefundOrder.Status.SUCCEEDED
        )
        self.assertEqual(
            payment.status, ActivityParticipationPaymentOrder.Status.REFUNDED
        )
        self.assertEqual(task.status, ScheduledTask.Status.SUCCEEDED)

    def test_activity_lifecycle_and_settlement_run_through_task_center(self):
        now = timezone.now()
        activity = self.make_activity(
            status=Activity.Status.FORMED,
            starts_at=now - timedelta(hours=3),
            ends_at=now - timedelta(hours=1),
            formation_deadline=now - timedelta(hours=4),
        )
        ActivityPublishOrder.objects.create(
            order_no="TASK-ACTIVITY-PUBLISH",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            status=ActivityPublishOrder.Status.PAID,
            expires_at=now - timedelta(days=1),
            paid_at=now - timedelta(days=1),
        )
        self.add_paid_participant(activity, now=now)
        register_activity_lifecycle_tasks(activity)

        lifecycle = process_due_tasks(now=now)

        activity.refresh_from_db()
        settlement = ActivitySettlement.objects.get(activity=activity)
        settlement_task = ScheduledTask.objects.get(
            task_type=ScheduledTask.Type.ACTIVITY_SETTLEMENT,
            business_key=str(activity.pk),
        )
        self.assertEqual(lifecycle["succeeded"], 3)
        self.assertEqual(activity.status, Activity.Status.COMPLETED)
        self.assertEqual(settlement.status, ActivitySettlement.Status.CONFIRMING)
        self.assertEqual(settlement_task.status, ScheduledTask.Status.PENDING)
        self.assertEqual(settlement_task.available_at, settlement.confirmation_deadline)

        confirming = process_due_tasks(now=settlement.confirmation_deadline)
        settlement.refresh_from_db()
        settlement_task.refresh_from_db()
        self.assertEqual(confirming["rescheduled"], 1)
        self.assertEqual(settlement.status, ActivitySettlement.Status.RISK_FROZEN)
        self.assertEqual(settlement_task.available_at, settlement.freeze_until)

        settled = process_due_tasks(now=settlement.freeze_until)
        settlement.refresh_from_db()
        settlement_task.refresh_from_db()
        self.assertEqual(settled["succeeded"], 1)
        self.assertEqual(settlement.status, ActivitySettlement.Status.SETTLED)
        self.assertEqual(settlement_task.status, ScheduledTask.Status.SUCCEEDED)

    def test_activity_task_synchronization_backfills_missing_tasks(self):
        activity = self.make_activity()
        participation = ActivityParticipation.objects.create(
            activity=activity,
            user=self.participant,
            status=ActivityParticipation.Status.PENDING_PAYMENT,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            payment_expires_at=timezone.now() + timedelta(minutes=30),
        )
        ActivityParticipationPaymentOrder.objects.create(
            order_no="TASK-ACTIVITY-PENDING",
            participation=participation,
            payer=self.participant,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            channel=ActivityParticipationPaymentOrder.Channel.MOCK_WECHAT,
            expires_at=participation.payment_expires_at,
        )

        first = synchronize_activity_tasks()
        second = synchronize_activity_tasks()

        self.assertEqual(first["payment_created"], 1)
        self.assertEqual(first["formation_created"], 1)
        self.assertEqual(first["start_created"], 1)
        self.assertEqual(first["completion_created"], 1)
        self.assertEqual(sum(second.values()), 0)

    def test_activity_task_sync_backfills_publish_expiry_and_refund_tasks(self):
        now = timezone.now()
        activity = self.make_activity()
        publish_order = ActivityPublishOrder.objects.create(
            order_no="TASK-ACTIVITY-PUBLISH-PENDING",
            activity=activity,
            payer=self.organizer,
            aa_principal_amount=4800,
            platform_service_fee_amount=480,
            payable_amount=5280,
            pricing_snapshot={"platform_service_fee_rate": "0.10"},
            expires_at=now + timedelta(minutes=30),
        )
        participation = self.add_paid_participant(activity, now=now)
        payment = participation.payment_orders.get()
        refund = ActivityParticipationRefundOrder.objects.create(
            idempotency_key="task-sync-activity-refund",
            activity=activity,
            participation=participation,
            payment_order=payment,
            beneficiary=self.participant,
            refund_type=(
                ActivityParticipationRefundOrder.RefundType.PARTICIPANT_CANCELLATION
            ),
            principal_refund_amount=4800,
            service_fee_refund_amount=480,
            refund_amount=5280,
            reason="测试补建活动退款任务",
        )

        first = synchronize_activity_tasks()
        second = synchronize_activity_tasks()

        self.assertEqual(first["publish_payment_created"], 1)
        self.assertEqual(first["refund_task_created"], 1)
        self.assertEqual(sum(second.values()), 0)
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.ACTIVITY_PUBLISH_PAYMENT_EXPIRY,
                business_key=publish_order.order_no,
            ).exists()
        )
        self.assertTrue(
            ScheduledTask.objects.filter(
                task_type=ScheduledTask.Type.ACTIVITY_PARTICIPATION_REFUND,
                business_key=refund.refund_no,
            ).exists()
        )
