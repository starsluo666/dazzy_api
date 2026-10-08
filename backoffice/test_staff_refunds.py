"""Staff authorization regressions. Isolated fixtures, no live money/channel requests."""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from activities.models import (
    ActivityAfterSalesCase,
    ActivityParticipationPaymentOrder,
    ActivityParticipationRefundOrder,
    ActivitySettlement,
)
from orders.models import (
    ProviderOrderDistribution,
    ProviderOrderRefundOrder,
    ProviderOrderSettlement,
)
from orders.services import create_provider_order_refund
from taskcenter.models import ScheduledTask
from . import tests as fixtures
from .models import AdminAuditLog, PlatformOperationSetting, ProviderOrderAfterSalesCase
from .refund_authorization import daily_usage


class ActivityFixture:
    create_activity = classmethod(
        fixtures.BackofficeActivityManagementTests.create_activity.__func__
    )


class StaffRefundTests(APITestCase):
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    @classmethod
    def setUpTestData(cls):
        fixtures.BackofficeProviderReviewTests.setUpTestData.__func__(cls)
        fixtures.BackofficeActivityManagementTests.setUpTestData.__func__(ActivityFixture)
        cls.activity = ActivityFixture.handan_activity
        cls.payment = ActivityParticipationPaymentOrder.objects.get(
            participation__activity=cls.activity
        )

    def setUp(self):
        self.client.force_authenticate(self.admin_user)
        self.role.permissions = [
            "dashboard.view",
            "order.after_sales.view",
            "order.after_sales.create",
            "order.after_sales.review",
            "order.finance.view",
            "order.finance.manage",
            "activity_finance.view",
            "activity_after_sales.create",
            "activity_after_sales.manage",
            "refund.approve",
        ]
        self.role.save()
        guard = patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("No live network allowed"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def limits(self, single=10000, daily=10000):
        setting = PlatformOperationSetting.current()
        setting.support_refund_single_limit, setting.support_refund_daily_limit = single, daily
        setting.save()

    def order(self, number="STAFF-REFUND", provider=None):
        return self.create_fulfillment_order(order_no=number, provider=provider or self.handan)

    def context(self, kind, reference):
        return self.client.get(reverse("admin-refund-context", args=(kind, reference)))

    def register(self, order, amount=1000):
        return self.client.post(
            reverse("backoffice-provider-order-after-sales"),
            {
                "order_no": order.order_no,
                "case_type": "refund",
                "requested_amount": amount,
                "reason": "客服核实订单争议后申请退款",
            },
            format="json",
        )

    def register_activity(self, principal=1000, fee=0):
        return self.client.post(
            reverse("admin-activity-refund-create", args=(self.payment.order_no,)),
            {
                "requested_principal_amount": principal,
                "requested_service_fee_amount": fee,
                "reason": "客服核实报名争议后申请退款",
            },
            format="json",
        )

    def action(self, case, action="approve", amount=1000, kind="provider"):
        name = (
            "backoffice-provider-order-after-sales-action"
            if kind == "provider"
            else "backoffice-activity-after-sales-action"
        )
        data = {"action": action, "result_note": "已经核实证据及用户诉求"}
        data.update(
            {"approved_amount": amount}
            if kind == "provider"
            else {"approved_principal_amount": amount, "approved_service_fee_amount": 0}
        )
        return self.client.post(reverse(name, args=(case,)), data, format="json")

    def new_case(self, order=None, amount=1000):
        response = self.register(order or self.order(), amount)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]["case_no"]

    def supervisor(self):
        self.role.permissions.append("refund.supervise")
        self.role.save()

    def test_registration_permission_separate_from_review_and_approval(self):
        order = self.order()
        self.role.permissions.remove("order.after_sales.create")
        self.role.save()
        self.assertEqual(self.register(order).status_code, 403)
        self.assertFalse(ProviderOrderAfterSalesCase.objects.exists())
        self.role.permissions.extend(["order.after_sales.create"])
        self.role.permissions.remove("refund.approve")
        self.role.save()
        case = self.new_case(order)
        self.assertEqual(self.action(case).status_code, 403)
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())
        self.assertEqual(self.action(case, "start_review").status_code, 200)

    def test_defaults_fail_closed_and_escalation_is_not_a_refund(self):
        case_no = self.new_case()
        response = self.action(case_no)
        self.assertEqual(response.status_code, 200, response.data)
        case = ProviderOrderAfterSalesCase.objects.get(case_no=case_no)
        self.assertTrue(case.requires_supervisor)
        self.assertEqual(case.status, "pending")
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())
        self.assertTrue(
            AdminAuditLog.objects.filter(action="refund.escalate", target_id=case_no).exists()
        )
        self.limits()
        self.assertEqual(self.action(case_no).status_code, 200)
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())
        self.assertEqual(self.action(case_no, "reject").status_code, 403)
        self.supervisor()
        self.assertEqual(self.action(case_no).status_code, 200)
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)

    def test_authorized_approval_exact_limit_and_duplicate_are_safe(self):
        self.limits(1000, 1000)
        case = self.new_case()
        self.assertEqual(self.action(case).status_code, 200)
        refund = ProviderOrderRefundOrder.objects.get()
        self.assertEqual(refund.refund_amount, 1000)
        self.assertEqual(refund.operator, self.admin_user)
        self.assertEqual(refund.status, "pending")
        self.assertEqual(self.action(case).status_code, 400)
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)
        self.assertEqual(
            self.client.get(reverse("admin-refund-policy")).data["data"]["daily_remaining"], 0
        )

    def test_single_order_limit_is_cumulative_across_operators(self):
        self.limits(1000, 10000)
        order = self.order()
        refund, _ = create_provider_order_refund(
            order_no=order.order_no,
            amount=700,
            source_type="system",
            source_reference=order.order_no,
            idempotency_key="old-refund",
            reason="之前的退款",
            operator=self.platform_admin,
        )
        ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(status="succeeded")
        case = self.new_case(order, amount=400)
        self.assertEqual(self.action(case, amount=400).status_code, 200)
        self.assertTrue(ProviderOrderAfterSalesCase.objects.get(case_no=case).requires_supervisor)
        self.assertEqual(order.refund_orders.count(), 1)

    def test_daily_limit_combines_activity_and_provider_and_reserves_failures(self):
        self.limits(10000, 1500)
        response = self.register_activity()
        self.assertEqual(response.status_code, 201, response.data)
        response = self.action(response.data["data"]["case_no"], kind="activity")
        self.assertEqual(response.status_code, 200, response.data)
        ActivityParticipationRefundOrder.objects.update(status="failed")
        self.assertEqual(daily_usage(self.admin_user), 1000)
        case = self.new_case(amount=600)
        response = self.action(case, amount=600)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["requires_supervisor"])
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())

    def test_daily_usage_uses_shanghai_midnight(self):
        self.limits()
        self.action(self.new_case())
        refund = ProviderOrderRefundOrder.objects.get()
        instant = datetime(2026, 10, 7, 16, 0, tzinfo=dt_timezone.utc)
        ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(
            created_at=instant - timedelta(seconds=1)
        )
        self.assertEqual(daily_usage(self.admin_user, now=instant - timedelta(seconds=1)), 1000)
        self.assertEqual(daily_usage(self.admin_user, now=instant), 0)

    def test_context_scope_pending_refund_and_no_writes(self):
        order, other = self.order(), self.order("OTHER-CITY", self.beijing)
        before = AdminAuditLog.objects.count()
        data = self.context("provider", order.order_no).data["data"]
        self.assertEqual(data["remaining"], 16800)
        self.assertTrue(data["can_create"])
        self.assertEqual(AdminAuditLog.objects.count(), before)
        self.assertEqual(self.context("provider", other.order_no).status_code, 404)
        self.assertEqual(self.register(other).status_code, 404)
        refund, _ = create_provider_order_refund(
            order_no=order.order_no,
            amount=700,
            source_type="system",
            source_reference=order.order_no,
            idempotency_key="reserved-refund",
            reason="处理中的退款",
        )
        ProviderOrderRefundOrder.objects.filter(pk=refund.pk).update(status="failed")
        data = self.context("provider", order.order_no).data["data"]
        self.assertEqual(data["occupied"], 700)
        self.assertEqual(data["remaining"], 16100)
        self.assertFalse(data["can_create"])
        self.assertEqual(self.register(order).status_code, 400)

    def settlement(self, order, status="settled"):
        return ProviderOrderSettlement.objects.create(
            order=order,
            provider=order.provider,
            paid_amount=16800,
            net_service_fee_amount=16800,
            net_transport_fee_amount=0,
            net_other_fee_amount=0,
            platform_commission_amount=5040,
            provider_service_income_amount=11760,
            provider_settlement_amount=11760,
            status=status,
            frozen_at=timezone.now(),
            freeze_until=timezone.now(),
        )

    def test_supervisor_does_not_bypass_settlement_or_distribution(self):
        self.supervisor()
        order = self.order()
        case = self.new_case(order)
        settlement = self.settlement(order)
        self.assertEqual(self.action(case).status_code, 400)
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())
        settlement.status = "risk_frozen"
        settlement.save()
        ProviderOrderDistribution.objects.create(
            settlement=settlement,
            req_seq_id="never-resend",
            req_date="20261007",
            status="failed",
            snapshot={},
            payment_fee_amount=59,
        )
        self.assertEqual(self.action(case).status_code, 400)
        self.assertFalse(self.context("provider", order.order_no).data["data"]["can_create"])
        self.assertFalse(ProviderOrderRefundOrder.objects.exists())

    def test_retry_requires_separate_permission_and_original_record(self):
        self.limits()
        case = self.new_case()
        self.action(case)
        refund = ProviderOrderRefundOrder.objects.get()
        refund.status = "failed"
        refund.save()
        self.assertEqual(self.action(case, "retry_refund").status_code, 403)
        response = self.client.post(
            reverse("backoffice-provider-order-refund-retry", args=(refund.refund_no,)),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)

    def test_task_center_cannot_bypass_refund_retry_permission(self):
        self.limits()
        self.action(self.new_case())
        refund = ProviderOrderRefundOrder.objects.get()
        task = ScheduledTask.objects.get(
            task_type="provider_order_refund", business_key=refund.refund_no
        )
        task.status = "failed"
        task.save()
        self.role.permissions.extend(["system.task.view", "system.task.retry"])
        self.role.save()
        url = reverse("backoffice-scheduled-task-retry", args=(task.public_id,))
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.status, "failed")
        self.role.permissions.append("refund.retry")
        self.role.save()
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ProviderOrderRefundOrder.objects.count(), 1)
        self.assertEqual(daily_usage(self.admin_user), 1000)

    def test_activity_escalation_and_retry_permissions(self):
        response = self.register_activity()
        case_no = response.data["data"]["case_no"]
        response = self.action(case_no, kind="activity")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["requires_supervisor"])
        self.assertFalse(ActivityParticipationRefundOrder.objects.exists())
        self.assertEqual(self.action(case_no, "reject", kind="activity").status_code, 403)
        self.supervisor()
        response = self.client.get(
            reverse("admin-work-items"), {"queue": "activity_refund_escalated"}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]["items"]), 1)
        self.assertEqual(self.action(case_no, kind="activity").status_code, 200)
        refund = ActivityParticipationRefundOrder.objects.get()
        refund.status = "failed"
        refund.save()
        url = reverse("backoffice-activity-refund-retry", args=(refund.refund_no,))
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 403)
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 1)

    def test_escalation_audit_keeps_proposal_without_recording_approval(self):
        case_no = self.new_case()
        self.action(case_no)
        audit = AdminAuditLog.objects.get(action="refund.escalate", target_id=case_no)
        self.assertEqual(audit.after["proposed_amount"], 1000)
        self.assertEqual(audit.after["result_note"], "已经核实证据及用户诉求")
        self.assertEqual(audit.after["single_limit"], 0)
        self.assertFalse(
            AdminAuditLog.objects.filter(
                action="order.after_sales.approve", target_id=case_no
            ).exists()
        )

    def test_limit_settings_require_platform_permission_and_integer_cents(self):
        url = reverse("backoffice-platform-operation-setting")
        self.assertEqual(
            self.client.patch(
                url, {"support_refund_single_limit": 1234}, format="json"
            ).status_code,
            403,
        )
        self.client.force_authenticate(self.platform_admin)
        response = self.client.patch(
            url,
            {"support_refund_single_limit": 1234, "support_refund_daily_limit": 5678},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["support_refund_single_limit"], 1234)
        for value in (-1, 0.5, 10_000_001):
            self.assertEqual(
                self.client.patch(
                    url, {"support_refund_single_limit": value}, format="json"
                ).status_code,
                400,
            )
        self.role.refresh_from_db()
        self.assertNotIn("refund.supervise", self.role.permissions)

    def test_activity_registration_trace_and_duplicate_and_approval(self):
        self.limits()
        response = self.register_activity(1000, 100)
        self.assertEqual(response.status_code, 201, response.data)
        case = ActivityAfterSalesCase.objects.get()
        self.assertEqual(case.created_by_operator, self.admin_user)
        self.assertEqual(case.applicant, self.payment.payer)
        self.assertEqual(case.requested_amount, 1100)
        self.assertFalse(ActivityParticipationRefundOrder.objects.exists())
        self.assertEqual(self.register_activity().status_code, 400)
        self.assertEqual(self.action(case.case_no, kind="activity").status_code, 200)
        self.payment.participation.refresh_from_db()
        self.assertEqual(self.payment.participation.status, "cancelled")
        refund = ActivityParticipationRefundOrder.objects.get()
        self.assertEqual(refund.status, "pending")
        self.assertEqual(refund.refund_amount, 1000)
        self.assertEqual(self.action(case.case_no, kind="activity").status_code, 400)
        self.assertEqual(ActivityParticipationRefundOrder.objects.count(), 1)

    def test_activity_registration_permissions_component_limits_and_city(self):
        self.assertEqual(self.register_activity(4801).status_code, 400)
        self.assertEqual(self.register_activity(0, 481).status_code, 400)
        self.assertEqual(self.register_activity(0, 0).status_code, 400)
        self.role.permissions.remove("activity_after_sales.create")
        self.role.save()
        self.assertEqual(self.register_activity().status_code, 403)
        self.role.permissions.append("activity_after_sales.create")
        self.role.save()
        self.activity.city_code = "110100"
        self.activity.save()
        self.assertEqual(self.register_activity().status_code, 404)
        self.assertEqual(self.context("activity", self.payment.order_no).status_code, 404)
        self.assertFalse(ActivityAfterSalesCase.objects.exists())

    def test_activity_supervisor_cannot_refund_settled_funds(self):
        response = self.register_activity()
        self.assertEqual(response.status_code, 201, response.data)
        case = response.data["data"]["case_no"]
        self.supervisor()
        ActivitySettlement.objects.create(
            activity=self.activity,
            beneficiary=self.activity.organizer,
            organizer_principal_amount=4800,
            participant_principal_amount=4800,
            retained_participant_principal_amount=0,
            settlement_amount=9600,
            platform_service_fee_amount=960,
            status="settled",
            confirmation_started_at=timezone.now(),
            confirmation_deadline=timezone.now(),
            freeze_until=timezone.now(),
        )
        self.assertTrue(
            ActivitySettlement.objects.filter(activity=self.activity, status="settled").exists()
        )
        self.assertEqual(self.action(case, kind="activity").status_code, 400)
        self.assertFalse(ActivityParticipationRefundOrder.objects.exists())

    def test_escalated_queue_is_supervisor_only_and_city_scoped(self):
        case = self.new_case()
        self.action(case)
        self.assertEqual(
            self.client.get(
                reverse("admin-work-items"), {"queue": "provider_refund_escalated"}
            ).status_code,
            404,
        )
        self.supervisor()
        response = self.client.get(
            reverse("admin-work-items"), {"queue": "provider_refund_escalated"}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["data"]["items"]), 1)
        self.assertEqual(self.action(case, "reject").status_code, 200)
        response = self.client.get(
            reverse("admin-work-items"), {"queue": "provider_refund_escalated"}
        )
        self.assertEqual(response.data["data"]["items"], [])
