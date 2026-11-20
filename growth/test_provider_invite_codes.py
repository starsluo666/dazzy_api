from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from accounts.models import User
from providers.models import ProviderProfile
from .models import ProviderInviteReward, ProviderInviteSource


class InviteCodeConstraintTests(TestCase):
    def test_unrelated_integrity_errors_are_not_retried_as_code_collisions(self):
        with patch("growth.provider_invite_models.invitation_code") as generate:
            with self.assertRaises(IntegrityError), transaction.atomic():
                ProviderInviteSource.objects.create(code="A7K9", kind="provider", provider_user=None, name="无归属达人")
        generate.assert_not_called()
        self.assertEqual(ProviderInviteSource.objects.count(), 0)


class InviteCodeConcurrencyTests(TransactionTestCase):
    def test_simultaneous_same_code_creation_is_retried_without_duplicates(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires PostgreSQL independent connections")
        barrier = Barrier(2)

        def create(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return ProviderInviteSource.objects.create(code="ZZZZ", kind="store", name=f"并发门店{index}").code
            finally:
                close_old_connections()

        with patch("growth.provider_invite_models.invitation_code", return_value="A7K9"), ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(create, (1, 2)))
        self.assertEqual(set(codes), {"ZZZZ", "A7K9"})
        self.assertEqual(ProviderInviteSource.objects.count(), 2)


class InviteCodeMigrationTests(TransactionTestCase):
    before = ("growth", "0002_providerinviteconfig_providerinvitesource_and_more")
    after = ("growth", "0005_reward_accounting_review")

    def test_existing_links_migrate_without_rebinding_or_changing_reward_snapshots(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([self.before])
            old_apps = executor.loader.project_state([self.before]).apps
            old_source = old_apps.get_model("growth", "ProviderInviteSource")
            source = old_source.objects.create(code="ABCDEF123456", kind="store", name="旧合作门店")
            another = old_source.objects.create(code="012345ABCDEF", kind="store", name="另一旧门店", active=False)
            user = User.objects.create_user(phone="13800009081")
            profile = ProviderProfile.objects.create(user=user, invitation_code=source.code)
            old_reward = old_apps.get_model("growth", "ProviderInviteReward")
            reward = old_reward.objects.create(
                source_id=source.pk, invitee_id=user.pk, profile_id=profile.pk,
                source_name_snapshot=source.name, code_snapshot=source.code,
                amount=5000, config_revision=1,
            )
            original_public_id = source.public_id
            MigrationExecutor(connection).migrate([self.after])
            migrated = ProviderInviteSource.objects.get(pk=source.pk)
            inactive = ProviderInviteSource.objects.get(pk=another.pk)
            self.assertRegex(migrated.code, r"^[A-HJ-NP-Z2-9]{4}$")
            self.assertRegex(inactive.code, r"^[A-HJ-NP-Z2-9]{4}$")
            self.assertNotEqual(migrated.code, inactive.code)
            self.assertEqual(migrated.public_id, original_public_id)
            self.assertEqual(migrated.legacy_code, "ABCDEF123456")
            self.assertEqual(inactive.legacy_code, "012345ABCDEF")
            self.assertFalse(inactive.active)
            reward = ProviderInviteReward.objects.get(pk=reward.pk)
            profile.refresh_from_db()
            self.assertEqual(reward.source_id, migrated.pk)
            self.assertEqual(reward.amount, 5000)
            self.assertEqual(reward.code_snapshot, "ABCDEF123456")
            self.assertEqual(profile.providerinviteattribution.source_id, migrated.pk)
            self.assertEqual(reward.payout_method, "manual")
            self.assertEqual(profile.invitation_code, "ABCDEF123456")
        finally:
            MigrationExecutor(connection).migrate([self.after])
