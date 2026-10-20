from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from backoffice import test_finance_work as fixtures
from backoffice.models import AdminWorkReadReceipt
from orders.models import ProviderBalancePaymentPool, ProviderOrderDistribution
from .models import FinanceRecoveryCase as Case, PaymentJobHeartbeat
from .watchdog import JOBS


class RecoveryApiTests(APITestCase):
    setUpTestData = classmethod(fixtures.FinanceWorkTests.setUpTestData.__func__)
    create_fulfillment_order = fixtures.FinanceWorkTests.create_fulfillment_order
    order = fixtures.FinanceWorkTests.order
    settlement = fixtures.FinanceWorkTests.settlement
    split = fixtures.FinanceWorkTests.split
    wallet = fixtures.FinanceWorkTests.wallet
    withdrawal = fixtures.FinanceWorkTests.withdrawal

    def setUp(self):
        fixtures.FinanceWorkTests.setUp(self)
        self.client.force_authenticate(self.platform_admin)
        self.reviewer = get_user_model().objects.create_superuser(phone="19900000791", password="test-only")
        self.record = self.split(status="unknown")

    def create(self, **changes):
        response = self.client.post(reverse("admin-finance-recovery"), {
            "kind": "distribution", "target_id": self.record.pk,
            "note": "核查原流水异常", **changes,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def act(self, row, action, **changes):
        return self.client.post(reverse("admin-finance-recovery-detail", args=[row["id"]]), {
            "revision": row["revision"], "action": action, "note": "核对原流水与内部凭证",
            "evidence_reference": "FIN-OFFLINE-1", **changes,
        }, format="json")

    def test_read_only_and_idempotent_registration(self):
        row = self.create()
        self.assertEqual(row["id"], self.create()["id"])
        self.assertEqual(Case.objects.count(), 1)
        before = list(ProviderOrderDistribution.objects.values())
        self.assertEqual(self.client.get(reverse("admin-finance-recovery")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin-finance-recovery-detail", args=[row["id"]])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin-payment-job-health")).status_code, 200)
        self.assertEqual(before, list(ProviderOrderDistribution.objects.values()))
        self.assertEqual(Case.objects.get().events.count(), 1)

    def test_platform_pool_pagination_and_exact_deep_link(self):
        for index in range(21):
            pool = ProviderBalancePaymentPool.objects.create(scope=f"offline-pool-{index}", hold_reason="暂停核查")
        url = reverse("admin-finance-recovery")
        first = self.client.get(url, {"view": "pools"}).data["data"]
        second = self.client.get(url, {"view": "pools", "page": 2}).data["data"]
        self.assertEqual(first["pools_total"], 21)
        self.assertEqual(len(first["pools"]), 20)
        self.assertEqual(len(second["pools"]), 1)
        exact = self.client.get(url, {"view": "pools", "work_id": pool.pk}).data["data"]
        self.assertEqual([row["id"] for row in exact["pools"]], [pool.pk])

    def test_two_person_record_only_does_not_clear_conflict_or_wallet(self):
        wallet = self.wallet(available_amount=7000, hold_reason="渠道差异")
        self.record.evidence_conflict_code = "query_terminal_conflict"
        self.record.save()
        row = self.create()
        row = self.act(row, "submit").data["data"]
        self.assertEqual(self.act(row, "approve").status_code, 400)
        self.client.force_authenticate(self.reviewer)
        response = self.act(row, "approve")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "resolved")
        self.record.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(self.record.evidence_conflict_code, "query_terminal_conflict")
        self.assertEqual(wallet.hold_reason, "渠道差异")
        self.assertEqual(wallet.available_amount, 7000)

    def test_changed_evidence_rejects_approval_and_revision_replay(self):
        row = self.create()
        submitted = self.act(row, "submit").data["data"]
        self.assertEqual(self.act(row, "submit").status_code, 400)
        self.record.status = "succeeded"
        self.record.save()
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.act(submitted, "approve").status_code, 400)
        response = self.act(submitted, "return")
        self.assertEqual(response.status_code, 200, response.data)

    def test_query_only_uses_original_reference_and_throttles(self):
        row = self.create()
        with patch("orders.distributions.query_distribution") as query:
            response = self.act(row, "query")
            self.assertEqual(response.status_code, 200, response.data)
            query.assert_called_once_with(self.record.settlement.order.order_no)
        self.assertEqual(self.act(response.data["data"], "query").status_code, 400)
        self.assertEqual(ProviderOrderDistribution.objects.count(), 1)

    def test_query_failure_is_logged_safely_and_requires_fresh_revision(self):
        row = self.create()
        with patch("orders.distributions.query_distribution", side_effect=ValueError("SECRET-KEY")):
            response = self.act(row, "query")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("SECRET", str(response.data))
        case = Case.objects.get()
        self.assertIsNone(case.lease_until)
        self.assertEqual(case.status, "open")
        self.assertEqual(case.revision, 2)
        self.assertNotIn("SECRET", str(list(case.events.values())))

    def test_permission_scope_and_secret_notes_are_rejected(self):
        self.role.permissions = [*self.role.permissions, "finance.recovery.view", "finance.recovery.manage", "finance.recovery.review"]
        self.role.save()
        self.client.force_authenticate(self.admin_user)
        self.assertEqual(self.client.get(reverse("admin-finance-recovery")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin-payment-job-health")).status_code, 403)
        self.client.force_authenticate(self.platform_admin)
        response = self.client.post(reverse("admin-finance-recovery"), {
            "kind": "distribution", "target_id": self.record.pk, "note": "https://secret.invalid/token",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_non_pool_resume_and_default_disabled_resume_rejected(self):
        row = self.create()
        with override_settings(HUIFU_FINANCE_RECOVERY_ENABLED=True):
            self.assertEqual(self.act(row, "submit", resolution="resume").status_code, 400)
        self.assertEqual(self.act(row, "submit", resolution="resume").status_code, 400)

    def test_work_queues_unread_dedupe_and_scope(self):
        row = self.create()
        pool = ProviderBalancePaymentPool.objects.create(scope="safe-test-scope", hold_reason="人工核查")
        for key in JOBS:
            PaymentJobHeartbeat.objects.update_or_create(key=key, defaults={"succeeded_at": timezone.now()})
        key = "taskcenter.process_due_scheduled_tasks"
        PaymentJobHeartbeat.objects.filter(key=key).update(succeeded_at=timezone.now() - timedelta(hours=1))
        response = self.client.get(reverse("admin-work-summary"))
        self.assertEqual(response.status_code, 200, response.data)
        todos = {t["key"]: t for t in response.data["data"]["todos"]}
        self.assertEqual(todos["finance_recovery_open"]["count"], 1)
        self.assertEqual(todos["payment_pool_hold"]["count"], 1)
        self.assertEqual(todos["payment_job_health"]["count"], 1)
        item = self.client.get(reverse("admin-work-items"), {"queue": "payment_job_health"}).data["data"]["items"][0]
        result = self.client.post(reverse("admin-work-items"), {k: item[k] for k in ("queue", "object_id", "event_version")}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(AdminWorkReadReceipt.objects.filter(queue_key="payment_job_health").count(), 1)
        again = self.client.get(reverse("admin-work-items"), {"queue": "payment_job_health"}).data["data"]["items"][0]
        self.assertTrue(again["read"])
        self.act(row, "submit")
        pool.refresh_from_db()
        self.assertEqual(pool.hold_reason, "人工核查")
        self.client.force_authenticate(self.admin_user)
        self.assertEqual(self.client.get(reverse("admin-work-items"), {"queue": "payment_job_health"}).status_code, 404)
