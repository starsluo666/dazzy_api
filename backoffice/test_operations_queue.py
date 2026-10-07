from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from notifications.models import UserNotification
from activities.models import ActivityAfterSalesCase, ActivityParticipation, ActivityReport
from orders.fulfillment import assess_timing, resolve_fulfillment_review
from orders.services import create_provider_order_refund
from supportcases.models import SupportCase, SupportCaseRecord
from taskcenter.models import ScheduledTask
from .models import AdminWorkReadReceipt
from . import tests as fixtures


class OperationsQueueTests(APITestCase):
    setUpTestData = classmethod(fixtures.BackofficeProviderReviewTests.setUpTestData.__func__)
    create_fulfillment_order = fixtures.BackofficeProviderReviewTests.create_fulfillment_order

    def setUp(self):
        self.role.permissions = [*self.role.permissions, "order.fulfillment.review", "support.case.view", "support.case.manage"]
        self.role.save()
        self.client.force_authenticate(self.admin_user)
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network permitted"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def summary(self):
        response = self.client.get(reverse("admin-work-summary"))
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def todo(self, key):
        return next(t for t in self.summary()["todos"] if t["key"] == key)

    def items(self, key, **query):
        response = self.client.get(reverse("admin-work-items"), {"queue": key, **query})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def read(self, item):
        return self.client.post(reverse("admin-work-items"), {
            key: item[key] for key in ("queue", "object_id", "event_version")
        }, format="json")

    def order(self, number, provider=None, **changes):
        order = self.create_fulfillment_order(order_no=number, provider=provider or self.handan)
        for key, value in changes.items():
            setattr(order, key, value)
        order.save()
        return order

    def test_live_summary_scope_and_same_overview_counts(self):
        self.order("WORK-LOCAL", fulfillment_review_required=True, fulfillment_revision=1)
        self.order("WORK-OTHER", provider=self.beijing, fulfillment_review_required=True, fulfillment_revision=1)
        todo = self.todo("fulfillment_review")
        self.assertEqual(todo["count"], 1)
        self.assertEqual(self.items("fulfillment_review")["items"][0]["reference"], "WORK-LOCAL")
        overview = self.client.get(reverse("backoffice-overview")).data["data"]
        self.assertEqual(next(t for t in overview["todos"] if t["key"] == todo["key"])["count"], 1)
        response = self.client.get(reverse("backoffice-provider-orders"), {"todo": todo["key"]})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)
        self.client.force_authenticate(self.platform_admin)
        self.assertEqual(self.todo("fulfillment_review")["count"], 2)

    def test_permission_removed_hides_queue_and_prevents_reading(self):
        self.order("WORK-PERM", fulfillment_review_required=True, fulfillment_revision=1)
        item = self.items("fulfillment_review")["items"][0]
        self.role.permissions = ["dashboard.view"]
        self.role.save()
        self.assertEqual(self.summary()["todos"], [])
        self.assertEqual(self.client.get(reverse("admin-work-items"), {"queue": "fulfillment_review"}).status_code, 404)
        self.assertEqual(self.read(item).status_code, 404)

    def test_reads_are_idempotent_per_person_and_revision_not_resolution(self):
        order = self.order("WORK-READ", fulfillment_review_required=True, fulfillment_revision=1)
        item = self.items("fulfillment_review")["items"][0]
        self.assertEqual(self.read(item).status_code, 200)
        self.assertEqual(self.read(item).status_code, 200)
        self.assertEqual(AdminWorkReadReceipt.objects.count(), 1)
        self.assertEqual(self.todo("fulfillment_review")["count"], 1)
        self.assertEqual(self.todo("fulfillment_review")["unread_count"], 0)
        order.refresh_from_db()
        self.assertTrue(order.fulfillment_review_required)
        self.client.force_authenticate(self.platform_admin)
        self.assertEqual(self.todo("fulfillment_review")["unread_count"], 1)
        self.client.force_authenticate(self.admin_user)
        order.fulfillment_revision += 1
        order.save()
        self.assertEqual(self.read(item).status_code, 404)
        self.assertEqual(self.todo("fulfillment_review")["unread_count"], 1)
        order.fulfillment_review_required = False
        order.save()
        self.assertEqual(self.todo("fulfillment_review")["count"], 0)

    def test_exact_link_filter_and_no_access_to_other_city(self):
        local = self.order("WORK-EXACT", fulfillment_review_required=True, fulfillment_revision=1)
        other = self.order("WORK-SECRET", provider=self.beijing, fulfillment_review_required=True, fulfillment_revision=1)
        item = self.items("fulfillment_review")["items"][0]
        self.assertEqual(item["target"]["query"]["work_id"], str(local.pk))
        response = self.client.get(reverse("backoffice-provider-orders"), item["target"]["query"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r["order_no"] for r in response.data["data"]["items"]], [local.order_no])
        self.assertEqual(self.read({**item, "object_id": str(other.pk)}).status_code, 404)
        self.assertEqual(self.client.get(reverse("backoffice-provider-orders"), {"todo": "fulfillment_review", "work_id": "oops"}).status_code, 404)

    def test_no_show_completed_refund_not_pending_work_and_legacy_no_duplicate(self):
        self.order("WORK-CLOSED", status="refunded", departure_timed_out_at=timezone.now(), fulfillment_review_required=True)
        legacy = self.order("WORK-LEGACY", status="pending_service", completion_submitted_at=None)
        self.assertEqual(self.todo("fulfillment_review")["count"], 0)
        self.assertEqual(self.todo("legacy_overdue")["count"], 1)
        legacy.fulfillment_review_required = True
        legacy.save()
        self.assertEqual(self.todo("legacy_overdue")["count"], 0)
        self.assertEqual(self.todo("fulfillment_review")["count"], 1)

    def test_refunds_pending_failed_exhausted_overdue_and_recovered(self):
        order = self.order("WORK-REFUND")
        refund, _ = create_provider_order_refund(order_no=order.order_no, amount=100, source_type="system",
            source_reference=order.order_no, idempotency_key="work-refund", reason="test")
        self.assertEqual(self.todo("provider_refund_attention")["count"], 0)
        task = ScheduledTask.objects.get(business_key=refund.refund_no)
        task.status = "failed"
        task.save()
        self.assertEqual(self.todo("provider_refund_attention")["count"], 1)
        response = self.client.get(reverse("backoffice-provider-order-finance"), {"todo": "provider_refund_attention", "record_type": "refund"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)
        task.status = "pending"
        task.save()
        type(refund).objects.filter(pk=refund.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.todo("provider_refund_attention")["count"], 1)
        refund.status = "succeeded"
        refund.save()
        self.assertEqual(self.todo("provider_refund_attention")["count"], 0)

    def test_support_reply_becomes_unread_without_count_change(self):
        case = SupportCase.objects.create(reporter=self.order_customer, case_type="complaint", target_type="general",
            reason="other", description="核查", city_code="130400")
        item = self.items("support_cases")["items"][0]
        self.assertEqual(self.read(item).status_code, 200)
        SupportCaseRecord.objects.create(case=case, actor=self.order_customer, record_type="user_reply", content="补充证据")
        self.assertEqual(self.todo("support_cases")["count"], 1)
        self.assertEqual(self.todo("support_cases")["unread_count"], 1)
        case.status = "resolved"
        case.save()
        self.assertEqual(self.todo("support_cases")["count"], 0)

    def test_overdue_escalation_changes_event_once(self):
        now = timezone.now()
        case = SupportCase.objects.create(reporter=self.order_customer, case_type="complaint", target_type="general",
            reason="other", description="核查", city_code="130400")
        SupportCase.objects.filter(pk=case.pk).update(created_at=now - timedelta(hours=3))
        self.assertEqual(self.read(self.items("support_cases")["items"][0]).status_code, 200)
        with patch("backoffice.operations_queue.timezone.now", return_value=now + timedelta(hours=2)):
            self.assertEqual(self.todo("support_cases")["unread_count"], 1)
            self.assertEqual(self.todo("support_cases")["overdue_count"], 1)
            self.assertEqual(self.read(self.items("support_cases")["items"][0]).status_code, 200)
            self.assertEqual(self.todo("support_cases")["unread_count"], 0)

    def test_inbox_pagination_and_unknown_queue(self):
        for index in range(25):
            SupportCase.objects.create(reporter=self.order_customer, case_type="consultation", target_type="general",
                reason="other", description=str(index), city_code="130400")
        first = self.items("support_cases")
        second = self.items("support_cases", page=2)
        self.assertEqual(first["total"], 25)
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(len(second["items"]), 5)
        self.assertEqual(self.client.get(reverse("admin-work-items"), {"queue": "secret"}).status_code, 404)

    def test_contact_alone_does_not_close_support_work(self):
        order = self.order("WORK-CONTACT", status="pending_support", support_contacted_at=timezone.now())
        self.assertEqual(self.todo("order_support")["count"], 1)
        order.status = "cancelled"
        order.save()
        self.assertEqual(self.todo("order_support")["count"], 0)

    def test_review_result_notifies_both_once_without_exposing_audit_notes(self):
        order = self.order("WORK-RESULT", status="in_service")
        assess_timing(order, stage="start", now=order.starts_at + timedelta(hours=2))
        order.save()
        revision = order.fulfillment_revision
        self.assertTrue(resolve_fulfillment_review(order, revision=revision, reason="内部核实依据，不外发", actor=self.admin_user))
        self.assertFalse(resolve_fulfillment_review(order, revision=revision, reason="重复核查", actor=self.admin_user))
        messages = UserNotification.objects.filter(target_id=order.order_no)
        self.assertEqual(messages.filter(title="订单履约核查已完成").count(), 2)
        self.assertEqual(messages.filter(title="订单履约需客服核实").count(), 2)
        self.assertFalse(messages.filter(content__contains="内部核实").exists())

    def test_non_admin_is_denied(self):
        self.client.force_authenticate(self.order_customer)
        self.assertEqual(self.client.get(reverse("admin-work-summary")).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(reverse("admin-work-summary")).status_code, 401)


class ActivityWorkQueueTests(APITestCase):
    setUpTestData = classmethod(fixtures.BackofficeActivityManagementTests.setUpTestData.__func__)
    create_activity = classmethod(fixtures.BackofficeActivityManagementTests.create_activity.__func__)
    create_failed_activity_refund = fixtures.BackofficeActivityManagementTests.create_failed_activity_refund
    summary = OperationsQueueTests.summary
    todo = OperationsQueueTests.todo
    items = OperationsQueueTests.items
    read = OperationsQueueTests.read

    def setUp(self):
        self.client.force_authenticate(self.admin_user)

    def test_activity_audit_reports_and_after_sales_scope_and_links(self):
        self.assertEqual(self.todo("activity_review")["count"], 1)
        # Bell works for operators without dashboard.view.
        self.assertEqual(self.client.get(reverse("backoffice-overview")).status_code, 403)
        for activity in (self.handan_activity, self.beijing_activity):
            ActivityReport.objects.create(activity=activity, reporter=self.participant,
                                          reason="safety_risk", description="核实现场")
        self.assertEqual(self.todo("activity_reports")["count"], 1)
        report = self.items("activity_reports")["items"][0]
        response = self.client.get(reverse("backoffice-activity-reports"), report["target"]["query"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)
        participation = ActivityParticipation.objects.get(activity=self.handan_activity, user=self.participant)
        case = ActivityAfterSalesCase.objects.create(participation=participation, applicant=self.participant,
            reason="other", description="申请核查", status="pending", requested_principal_amount=4800,
            requested_service_fee_amount=480, requested_amount=5280)
        self.assertEqual(self.todo("activity_after_sales")["count"], 1)
        target = self.items("activity_after_sales")["items"][0]["target"]
        response = self.client.get(reverse("backoffice-activity-finance"), target["query"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["items"][0]["case_no"], case.case_no)

    def test_refund_retry_failure_is_a_new_event_and_success_clears_work(self):
        refund, task = self.create_failed_activity_refund()
        item = self.items("activity_refund_attention")["items"][0]
        self.assertEqual(self.read(item).status_code, 200)
        self.assertEqual(self.todo("activity_refund_attention")["unread_count"], 0)
        task.finished_at += timedelta(minutes=5)
        task.save()
        self.assertEqual(self.todo("activity_refund_attention")["unread_count"], 1)
        target = self.items("activity_refund_attention")["items"][0]["target"]
        response = self.client.get(reverse("backoffice-activity-finance"), target["query"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["items"][0]["refund_no"], refund.refund_no)
        refund.status = "succeeded"
        refund.save()
        self.assertEqual(self.todo("activity_refund_attention")["count"], 0)
