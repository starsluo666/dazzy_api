"""Read-only phase-two diagnostics. All fixtures are local, all HTTP is forbidden."""
from datetime import timedelta
import uuid

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from notifications.external_channels import NotificationEnvelope, get_external_channel
from orders.models import ProviderOrderDistribution, ProviderOrderDistributionPreflight, ProviderOrderSettlement
from providers.models import ProviderIncomeEntry, ProviderIncomeWallet, ProviderWithdrawal
from taskcenter.models import ScheduledTask
from . import test_operations_queue as fixtures


class FinanceWorkTests(APITestCase):
    setUpTestData = classmethod(fixtures.OperationsQueueTests.setUpTestData.__func__)
    create_fulfillment_order = fixtures.OperationsQueueTests.create_fulfillment_order
    order = fixtures.OperationsQueueTests.order
    summary = fixtures.OperationsQueueTests.summary
    todo = fixtures.OperationsQueueTests.todo
    items = fixtures.OperationsQueueTests.items
    read = fixtures.OperationsQueueTests.read

    def setUp(self):
        fixtures.OperationsQueueTests.setUp(self)
        self.role.permissions = [*self.role.permissions, "order.finance.view", "order.finance.manage", "system.task.view"]
        self.role.save()

    def settlement(self, number="SPLIT-TEST", provider=None):
        order = self.order(number, provider=provider)
        now = timezone.now()
        return ProviderOrderSettlement.objects.create(order=order, provider=order.provider,
            paid_amount=10000, net_service_fee_amount=10000, net_transport_fee_amount=0, net_other_fee_amount=0,
            platform_commission_amount=3000, provider_service_income_amount=7000, provider_settlement_amount=7000,
            status="settled", frozen_at=now, freeze_until=now)

    def split(self, number="SPLIT-TEST", provider=None, **changes):
        defaults = {"status": "processing", "snapshot": {"income_mode": "manual_cash_v1", "provider_amount": 7000},
                    "payment_fee_amount": 35}
        return ProviderOrderDistribution.objects.create(settlement=self.settlement(number, provider), req_seq_id=number,
            req_date="20261007", **{**defaults, **changes})

    def wallet(self, provider=None, **changes):
        return ProviderIncomeWallet.objects.create(provider=provider or self.handan, channel_scope="test-only",
            receiver_id="DO-NOT-EXPOSE", **changes)

    def withdrawal(self, status="processing", wallet=None, reserve=True, **changes):
        wallet = wallet or self.wallet()
        record = ProviderWithdrawal.objects.create(wallet=wallet, req_seq_id=uuid.uuid4().hex, req_date="20261007",
            request_key=uuid.uuid4(), amount=1000, status=status, snapshot={"token_no": "SECRET-TEST-TOKEN"}, **changes)
        if reserve:
            ProviderIncomeEntry.objects.create(wallet=wallet, withdrawal=record, source_key=f"reserve:{record.pk}",
                kind="reserve", amount=1000, available_delta=-1000, reserved_delta=1000)
        return record

    def finance(self, queue, **params):
        response = self.client.get(reverse("admin-finance-work"), {"queue": queue, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_split_unknown_conflict_stall_and_terminal_cleanup(self):
        split = self.split()
        self.assertEqual(self.todo("distribution_attention")["count"], 0)
        ProviderOrderDistribution.objects.filter(pk=split.pk).update(created_at=timezone.now() - timedelta(minutes=31))
        self.assertEqual(self.todo("distribution_attention")["count"], 1)
        split.status = "unknown"
        split.save()
        item = self.items("distribution_attention")["items"][0]
        self.assertEqual(self.read(item).status_code, 200)
        split.last_queried_at = timezone.now()
        split.save()
        self.assertEqual(self.todo("distribution_attention")["unread_count"], 0)
        split.status = "succeeded"
        split.evidence_conflict_code = "query_terminal_conflict"
        split.save()
        self.assertEqual(self.todo("distribution_attention")["unread_count"], 1)
        split.evidence_conflict_code = ""
        split.save()
        self.assertEqual(self.todo("distribution_attention")["count"], 0)

    def test_preflight_only_actionable_failures_and_repeat_dedupe(self):
        preflight = ProviderOrderDistributionPreflight.objects.create(settlement=self.settlement(), status="deferred",
            reason_code="payment_proof", failure_count=2, checked_at=timezone.now())
        self.assertEqual(self.todo("distribution_preflight_attention")["count"], 0)
        preflight.failure_count = 3
        preflight.save()
        self.assertEqual(self.todo("distribution_preflight_attention")["count"], 1)
        item = self.items("distribution_preflight_attention")["items"][0]
        self.assertEqual(self.read(item).status_code, 200)
        preflight.failure_count = 4
        preflight.checked_at = timezone.now()
        preflight.save()
        self.assertEqual(self.todo("distribution_preflight_attention")["unread_count"], 0)
        self.assertEqual(self.finance("distribution_preflight_attention")["total"], 1)
        for reason in ("configuration", "local_conditions"):
            preflight.reason_code = reason
            preflight.save()
            self.assertEqual(self.todo("distribution_preflight_attention")["count"], 0)
        preflight.status = "registered"
        preflight.save()
        self.assertEqual(self.todo("distribution_preflight_attention")["count"], 0)

    def test_credit_gap_excludes_legacy_and_zero_and_does_not_credit(self):
        split = self.split(status="succeeded")
        legacy = self.split("OLD-SPLIT", status="succeeded", snapshot={"provider_amount": 7000})
        zero = self.split("ZERO-SPLIT", status="succeeded", snapshot={"income_mode": "manual_cash_v1", "provider_amount": 0})
        ProviderOrderDistribution.objects.filter(pk__in=(split.pk, legacy.pk, zero.pk)).update(updated_at=timezone.now() - timedelta(minutes=6))
        self.assertEqual(self.todo("distribution_credit_attention")["count"], 1)
        self.assertEqual(ProviderIncomeWallet.objects.count(), 0)
        wallet = self.wallet(available_amount=7000)
        ProviderIncomeEntry.objects.create(wallet=wallet, source_key="credit-test", distribution=split,
            kind="credit", amount=7000, available_delta=7000, reserved_delta=0)
        self.assertEqual(self.todo("distribution_credit_attention")["count"], 0)
        self.assertEqual(self.todo("income_reconciliation")["count"], 0)

    def test_withdrawal_waiting_and_failed_return_are_not_failures(self):
        record = self.withdrawal()
        self.assertEqual(self.todo("withdrawal_attention")["count"], 0)
        ProviderWithdrawal.objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(hours=73))
        self.assertEqual(self.todo("withdrawal_attention")["count"], 1)
        record.status = "failed"
        record.save()
        self.assertEqual(self.todo("withdrawal_attention")["count"], 1)  # missing release
        ProviderIncomeEntry.objects.create(wallet=record.wallet, withdrawal=record, source_key="returned",
            kind="release", amount=1000, available_delta=1000, reserved_delta=-1000)
        self.assertEqual(self.todo("withdrawal_attention")["count"], 0)
        record.status = "attention"
        record.save()
        self.assertEqual(self.todo("withdrawal_attention")["count"], 1)

    def test_wallet_reconciliation_is_read_only_and_holds_are_sticky(self):
        wallet = self.wallet(available_amount=1000)
        before = list(ProviderIncomeWallet.objects.values())
        self.assertEqual(self.todo("income_reconciliation")["count"], 1)
        data = self.finance("income_reconciliation")
        self.assertEqual(data["items"][0]["checks"][0], {"label": "可用余额", "actual": 1000, "expected": 0})
        self.assertNotIn("DO-NOT-EXPOSE", str(data))
        self.assertEqual(list(ProviderIncomeWallet.objects.values()), before)
        self.assertEqual(ProviderIncomeEntry.objects.count(), 0)
        wallet.available_amount = 0
        wallet.hold_reason = "test hold"
        wallet.save()
        self.assertEqual(self.todo("income_reconciliation")["count"], 1)
        wallet.hold_reason = ""
        wallet.save()
        self.assertEqual(self.todo("income_reconciliation")["count"], 0)

    def test_scope_exact_target_permission_and_private_data(self):
        self.split("LOCAL-SPLIT", status="unknown")
        foreign = self.split("OTHER-CITY", provider=self.beijing, status="unknown")
        self.assertEqual(self.todo("distribution_attention")["count"], 1)
        item = self.items("distribution_attention")["items"][0]
        self.assertEqual(item["target"]["page"], "finance_alerts")
        data = self.finance("distribution_attention", work_id=item["object_id"], search=item["reference"])
        self.assertEqual(data["total"], 1)
        self.assertEqual(self.finance("distribution_attention", work_id=foreign.pk)["total"], 0)
        withdrawal = self.withdrawal(status="unknown")
        data = self.finance("withdrawal_attention", work_id=withdrawal.pk)
        self.assertNotIn("SECRET-TEST-TOKEN", str(data))
        self.assertNotIn("snapshot", str(data))
        self.role.permissions = ["dashboard.view", "order.finance.view"]
        self.role.save()
        self.assertFalse(any(t["group"] == "finance" for t in self.summary()["todos"]))
        self.assertEqual(self.client.get(reverse("admin-finance-work"), {"queue": "withdrawal_attention"}).status_code, 403)
        self.assertEqual(self.read(item).status_code, 404)

    def test_failed_stale_and_retry_tasks_scope_and_link(self):
        local = self.order("TASK-LOCAL")
        foreign = self.order("TASK-FOREIGN", provider=self.beijing)
        now = timezone.now()
        for order in (local, foreign):
            ScheduledTask.objects.create(task_type="provider_departure_timeout", business_type="provider_order",
                business_key=order.order_no, dedupe_key=f"test:{order.order_no}", status="failed", scheduled_at=now,
                available_at=now, attempt_count=3, finished_at=now)
        self.assertEqual(self.todo("critical_tasks")["count"], 1)
        item = self.items("critical_tasks")["items"][0]
        response = self.client.get(reverse("backoffice-scheduled-tasks"), item["target"]["query"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["pagination"]["total"], 1)
        self.assertEqual(self.read(item).status_code, 200)
        task = ScheduledTask.objects.get(dedupe_key=f"test:{local.order_no}")
        task.status, task.available_at = "pending", now + timedelta(minutes=1)
        task.save()
        self.assertEqual(self.todo("critical_tasks")["count"], 0)
        task.available_at = now - timedelta(minutes=6)
        task.save()
        self.assertEqual(self.todo("critical_tasks")["unread_count"], 1)
        task.status = "running"
        task.started_at = now - timedelta(minutes=6)
        task.save()
        self.assertEqual(self.todo("critical_tasks")["count"], 1)
        task.status = "succeeded"
        task.save()
        self.assertEqual(self.todo("critical_tasks")["count"], 0)

    def test_reserved_channel_api_is_read_only(self):
        response = self.client.get(reverse("admin-notification-channels"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c["key"] for c in response.data["data"]["channels"] if c["enabled"]], ["admin_inbox"])
        self.assertEqual(self.client.post(reverse("admin-notification-channels"), {}).status_code, 405)
        self.assertEqual(self.client.post(reverse("admin-finance-work"), {}).status_code, 405)
        self.client.force_authenticate(self.order_customer)
        self.assertEqual(self.client.get(reverse("admin-notification-channels")).status_code, 403)


class ReservedChannelTests(SimpleTestCase):
    def test_stub_cannot_send_and_has_no_delivery_receipt(self):
        event = NotificationEnvelope("example:1:revision:1", "internal-operator", "work_attention", "finance_alerts")
        for name in ("sms", "wechat"):
            result = get_external_channel(name).send(event)
            self.assertEqual(result.status, "not_configured")
            self.assertEqual(result.provider_reference, "")
        with self.assertRaises(ValueError):
            get_external_channel("arbitrary-webhook")
