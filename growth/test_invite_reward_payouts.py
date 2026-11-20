from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import User
from orders.models import ProviderBalancePaymentPool, ProviderOrder, ProviderOrderSettlement
from providers.models import ProviderIncomeEntry, ProviderIncomeWallet, ProviderProfile, ProviderService, ServiceCategory

from .first_order_rewards import record_first_order_reward
from .invite_reward_payouts import _observe, query_reward, submit_reward
from .models import ProviderInviteAttribution, ProviderInvitePayout, ProviderInviteReward, ProviderInviteSource


class InviteRewardSettlementTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(phone="13910006001")
        self.customer = User.objects.create_user(phone="13910006002")
        self.recipient = ProviderProfile.objects.create(user=self.owner, status="store")
        self.invitee = ProviderProfile.objects.create(user=User.objects.create_user(phone="13910006003"))
        self.source = ProviderInviteSource.objects.create(kind="store", name="测试门店", store_user=self.owner)
        self.attribution = ProviderInviteAttribution.objects.create(
            profile=self.invitee, source=self.source, code_snapshot=self.source.code,
            source_name_snapshot=self.source.name, config_revision=1,
            first_order_rule={"enabled": True, "mode": "percentage", "amount": 0, "rate_bps": 1000},
        )
        category = ServiceCategory.objects.create(name="测试服务", slug="invite-services")
        self.service = ProviderService.objects.create(
            provider=self.invitee, category=category, billing_type="per_session", price_amount=10000,
        )

    def settlement(self, order_no, *, refunded=0, status="pending_review"):
        now = timezone.now()
        order = ProviderOrder.objects.create(
            order_no=order_no, customer=self.customer, provider=self.invitee, service=self.service,
            provider_name_snapshot="测试达人", service_name_snapshot="测试服务",
            billing_type_snapshot="per_session", unit_price_amount=10000, starts_at=now - timedelta(days=5),
            ends_at=now - timedelta(days=5, hours=-1), duration_minutes=60, meeting_address="测试地点",
            contact_name="测试用户", contact_phone=self.customer.phone,
            service_fee_amount=10000, transport_fee_amount=1000, discount_amount=2000, payable_amount=9000,
            payment_expires_at=now - timedelta(days=5, hours=1), paid_at=now - timedelta(days=5, hours=2),
            status=status, customer_confirmed_at=now - timedelta(days=4),
        )
        return ProviderOrderSettlement.objects.create(
            order=order, provider=self.invitee, status="settled", paid_amount=9000,
            refunded_amount=refunded, net_service_fee_amount=8000, net_transport_fee_amount=1000 - refunded,
            net_other_fee_amount=0, platform_commission_amount=1600, provider_service_income_amount=6400,
            provider_settlement_amount=7400 - refunded, frozen_at=now - timedelta(days=4),
            freeze_until=now - timedelta(days=1), settled_at=now,
        )

    def test_first_order_needs_valid_settlement_but_does_not_wait_for_review(self):
        invalid = self.settlement("INVITE-REFUNDED", refunded=100)
        first = self.settlement("INVITE-FIRST")
        other = self.settlement("INVITE-OTHER")
        with transaction.atomic():
            for settlement in (invalid, first, other, first):
                record_first_order_reward(settlement)
        reward = ProviderInviteReward.objects.get()
        self.assertEqual(reward.kind, "first_order")
        self.assertEqual(reward.amount, 800)
        self.assertEqual(reward.order_id, first.order_id)
        self.assertEqual(reward.source_id, self.source.pk)
        self.attribution.refresh_from_db()
        self.assertEqual(self.attribution.first_order_id, first.order_id)

    def test_fixed_reward_uses_snapshot_and_can_coexist_with_registration_reward(self):
        self.attribution.first_order_rule = {"enabled": True, "mode": "fixed", "amount": 1750, "rate_bps": 0}
        self.attribution.save()
        ProviderInviteReward.objects.create(
            profile=self.invitee, invitee=self.invitee.user, source=self.source,
            source_name_snapshot=self.source.name, code_snapshot=self.source.code, amount=500, config_revision=1,
        )
        with transaction.atomic():
            record_first_order_reward(self.settlement("INVITE-FIXED"))
        self.assertEqual(
            dict(ProviderInviteReward.objects.values_list("kind", "amount")),
            {"registration": 500, "first_order": 1750},
        )

    def test_channel_acceptance_never_credits_cash_and_duplicate_queries_credit_once(self):
        reward = ProviderInviteReward.objects.create(
            profile=self.invitee, invitee=self.invitee.user, source=self.source,
            source_name_snapshot=self.source.name, code_snapshot=self.source.code,
            amount=800, config_revision=1, payout_method="balance", status="approved",
        )
        payout = ProviderInvitePayout.objects.create(
            reward=reward, pool=ProviderBalancePaymentPool.objects.create(scope="invite-test"),
            req_date="20261121", req_seq_id="IRTEST",
            snapshot={"intent": {"amount": 800, "receiver_id": "HUIFU-TEST-USER"},
                      "receiver_scope": "test", "receiver_profile": self.recipient.pk, "fee_reserve": 5},
        )
        success = {"status": "succeeded", "gateway_trade_no": "TEST-CREDIT-1", "channel_fee_amount": 2}
        for result in ({"status": "unknown"}, success):
            _observe(payout.pk, result, queried=False)
            self.assertFalse(ProviderIncomeWallet.objects.exists())
        _observe(payout.pk, success, queried=True)
        _observe(payout.pk, success, queried=True)
        reward.refresh_from_db()
        payout.refresh_from_db()
        self.assertEqual(reward.status, "paid")
        self.assertTrue(payout.funds_released)
        self.assertEqual(ProviderIncomeWallet.objects.get(provider=self.recipient).available_amount, 800)
        self.assertEqual(ProviderIncomeEntry.objects.filter(source_key=f"invite-reward:{reward.pk}").count(), 1)

    def test_conflicting_channel_evidence_requires_manual_reconciliation(self):
        reward = ProviderInviteReward.objects.create(
            profile=self.invitee, invitee=self.invitee.user, source=self.source,
            source_name_snapshot=self.source.name, code_snapshot=self.source.code,
            amount=800, config_revision=1, payout_method="balance", status="approved",
        )
        payout = ProviderInvitePayout.objects.create(
            reward=reward, pool=ProviderBalancePaymentPool.objects.create(scope="invite-test"),
            req_date="20261121", req_seq_id="IRTEST-CONFLICT",
            snapshot={"intent": {"amount": 800, "receiver_id": "HUIFU-TEST-USER"},
                      "receiver_scope": "test", "receiver_profile": self.recipient.pk, "fee_reserve": 5},
        )
        _observe(payout.pk, {"status": "unknown", "conflict": True, "response_digest": "evidence"}, queried=True)
        _observe(payout.pk, {
            "status": "succeeded", "gateway_trade_no": "TEST-CHANNEL-TRADE", "channel_fee_amount": 5,
        }, queried=True)
        payout.refresh_from_db()
        reward.refresh_from_db()
        self.assertEqual(payout.status, "review")
        self.assertEqual(payout.response_digest, "evidence")
        self.assertFalse(payout.funds_released)
        self.assertTrue(payout.pool.hold_reason)
        self.assertTrue(reward.payout_blocker)
        self.assertEqual(reward.status, "approved")
        self.assertFalse(ProviderIncomeWallet.objects.exists())

    def test_payout_task_queries_recorded_requests_even_when_new_payments_are_disabled(self):
        from .tasks import process_invite_reward_payouts

        reward = ProviderInviteReward.objects.create(
            profile=self.invitee, invitee=self.invitee.user, source=self.source,
            source_name_snapshot=self.source.name, code_snapshot=self.source.code,
            amount=800, config_revision=1, payout_method="balance", status="approved",
        )
        payout = ProviderInvitePayout.objects.create(
            reward=reward, pool=ProviderBalancePaymentPool.objects.create(scope="invite-test"),
            req_date="20261121", req_seq_id="IRTEST-PENDING", snapshot={}, status="unknown",
        )
        ProviderInvitePayout.objects.filter(pk=payout.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        with (
            self.settings(HUIFU_INVITE_REWARD_PAYOUT_ENABLED=False),
            patch("growth.tasks.query_reward") as query,
            patch("growth.tasks.submit_reward") as submit,
        ):
            process_invite_reward_payouts()
        query.assert_called_once_with(payout)
        submit.assert_not_called()


@override_settings(
    HUIFU_INVITE_REWARD_PAYOUT_ENABLED=True,
    HUIFU_INVITE_REWARD_PAYOUT_CONFIRMED=True,
    HUIFU_PROVIDER_PLATFORM_FEE_POLICY_CONFIRMED=True,
    HUIFU_INVITE_REWARD_PAYOUT_MAX_CENTS=2000,
    HUIFU_INVITE_REWARD_PAYOUT_ACCT_ID="TEST_BASIC",
    HUIFU_INVITE_REWARD_PAYOUT_RISK_IP="192.0.2.5",
    HUIFU_INVITE_REWARD_PAYOUT_TRANSFER_TYPE="01",
    HUIFU_INVITE_REWARD_PAYOUT_FEE_MODE="merchant_payer",
    HUIFU_INVITE_REWARD_PAYOUT_FEE_RESERVE_CENTS=5,
)
class InviteRewardChannelTests(TransactionTestCase):
    def test_timeout_keeps_one_immutable_request_until_original_query_confirms(self):
        owner = User.objects.create_user(phone="13910006008")
        recipient = ProviderProfile.objects.create(user=owner, status="approved")
        invitee = User.objects.create_user(phone="13910006009")
        invited = ProviderProfile.objects.create(user=invitee)
        source = ProviderInviteSource.objects.create(kind="provider", provider_user=owner, name="测试达人")
        reward = ProviderInviteReward.objects.create(
            profile=invited, invitee=invitee, source=source, source_name_snapshot=source.name,
            code_snapshot=source.code, amount=800, config_revision=1, payout_method="balance",
            status="approved",
        )
        config = SimpleNamespace(environment="prod", merchant_id="TEST_MERCHANT", sys_id="TEST_SYS", product_id="TEST_PRODUCT")
        account = SimpleNamespace(user_huifu_id="TEST_USER", channel_scope="test-receiver")

        def verify_account(*args, **kwargs):
            account.channel_checked_at = timezone.now()
            return account

        with (
            patch("growth.invite_reward_payouts.HuifuPaymentConfig.from_settings", return_value=config),
            patch("growth.invite_reward_payouts.refresh_onboarding"),
            patch("growth.invite_reward_payouts.manual_cash_account", side_effect=verify_account),
            patch("growth.invite_reward_payouts.HuifuBalancePaymentGateway") as gateway_class,
        ):
            gateway = gateway_class.return_value
            gateway.platform_balance.return_value = {"available_amount": 805, "response_digest": "synthetic"}
            gateway.pay.side_effect = TimeoutError
            self.assertTrue(submit_reward(reward.pk))
            self.assertFalse(submit_reward(reward.pk))
            payout = ProviderInvitePayout.objects.get(reward=reward)
            self.assertEqual(payout.status, "unknown")
            self.assertFalse(ProviderIncomeWallet.objects.exists())
            intent = gateway.pay.call_args.args[0]
            self.assertEqual(payout.req_seq_id, intent.req_seq_id)
            gateway.query.return_value = {
                "status": "succeeded", "gateway_trade_no": "TEST-CHANNEL-TRADE", "channel_fee_amount": 5,
            }
            query_reward(payout)
            gateway.query.assert_called_once_with(intent, known_trade_no="")
            gateway.pay.assert_called_once()
        self.assertEqual(ProviderIncomeWallet.objects.get(provider=recipient).available_amount, 800)
        payout.refresh_from_db()
        self.assertTrue(payout.funds_released)
