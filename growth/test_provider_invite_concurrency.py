"""PostgreSQL row-lock regression; SQLite cannot verify financial concurrency."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import connections
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from backoffice.models import AdminAuditLog
from growth.models import ProviderInviteReward, ProviderInviteSource
from providers.models import ProviderProfile


@skipUnlessDBFeature("has_select_for_update")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ProviderInviteConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(phone="13800009101", password="test-password")
        invitee = User.objects.create_user(phone="13800009102", password="test-password")
        profile = ProviderProfile.objects.create(user=invitee, status="pending")
        source = ProviderInviteSource.objects.create(kind="store", name="并发测试门店")
        self.reward = ProviderInviteReward.objects.create(
            source=source,
            invitee=invitee,
            profile=profile,
            source_name_snapshot=source.name,
            code_snapshot=source.code,
            amount=5000,
            config_revision=1,
        )

    def race(self, payload):
        barrier = Barrier(2)

        def run():
            connections.close_all()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=self.admin.pk))
                barrier.wait(timeout=10)
                return client.post(
                    f"/api/v1/admin/provider-invites/rewards/{self.reward.public_id}/action/",
                    payload,
                    format="json",
                ).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            return sorted(future.result(timeout=30) for future in futures)

    def test_simultaneous_reviews_only_transition_once(self):
        self.assertEqual(
            self.race(
                {
                    "action": "approve",
                    "revision": 0,
                    "confirmed": True,
                    "note": "真实有效的新注册核验",
                }
            ),
            [200, 400],
        )
        self.assertEqual(AdminAuditLog.objects.filter(action="provider_invite.approve").count(), 1)

    def test_simultaneous_payout_recording_only_transition_once(self):
        self.reward.status = "approved"
        self.reward.reviewed_by = self.admin
        self.reward.reviewed_at = timezone.now()
        self.reward.save()
        self.assertEqual(
            self.race(
                {
                    "action": "paid",
                    "revision": 0,
                    "confirmed": True,
                    "note": "已核对收款人并线下打款",
                    "transfer_reference": "concurrent-receipt",
                    "paid_at": timezone.now().isoformat(),
                }
            ),
            [200, 400],
        )
        self.assertEqual(AdminAuditLog.objects.filter(action="provider_invite.paid").count(), 1)
