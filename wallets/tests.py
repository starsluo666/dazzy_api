from datetime import timedelta
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User

from .models import (
    RechargeCampaign,
    RechargeDiscountTier,
    UserWallet,
    WalletLedgerEntry,
    WalletPaymentAllocation,
    WalletRechargeOrder,
)
from .services import (
    _credit_recharge_order,
    complete_wallet_refund,
    consume_wallet_payment,
    create_recharge_order,
    prepare_wallet_payment,
    preview_wallet_payment,
    recharge_pricing,
    release_wallet_payment,
    confirm_recharge_payment,
)


class WalletServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            phone="13800009991", password="test-password"
        )
        self.wallet = UserWallet.objects.create(
            user=self.user, available_balance=150_000
        )

    def test_recharge_quantity_uses_highest_matching_discount_tier(self):
        campaign = RechargeCampaign.objects.create(
            is_enabled=True,
            unit_face_amount=100_000,
            max_quantity_per_order=5,
        )
        RechargeDiscountTier.objects.create(
            campaign=campaign, min_quantity=1, discount_rate_bps=9800
        )
        RechargeDiscountTier.objects.create(
            campaign=campaign, min_quantity=2, discount_rate_bps=9500
        )

        one = recharge_pricing(campaign=campaign, quantity=1)
        two = recharge_pricing(campaign=campaign, quantity=2)

        self.assertEqual(one["credited_amount"], 100_000)
        self.assertEqual(one["payable_amount"], 98_000)
        self.assertEqual(two["credited_amount"], 200_000)
        self.assertEqual(two["payable_amount"], 190_000)

    def test_balance_sufficient_forces_full_wallet_payment(self):
        preview = preview_wallet_payment(
            user_id=self.user.pk,
            payable_amount=100_000,
            business_type=WalletPaymentAllocation.BusinessType.PROVIDER_ORDER,
            business_order_no="ORDER-FULL",
        )
        self.assertTrue(preview.balance_sufficient)
        self.assertEqual(preview.wallet_amount, 100_000)
        self.assertEqual(preview.external_amount, 0)

        allocation = prepare_wallet_payment(
            user_id=self.user.pk,
            payable_amount=100_000,
            business_type=WalletPaymentAllocation.BusinessType.PROVIDER_ORDER,
            business_order_no="ORDER-FULL",
        )
        self.wallet.refresh_from_db()
        self.assertEqual(allocation.wallet_amount, 100_000)
        self.assertEqual(allocation.external_amount, 0)
        self.assertEqual(self.wallet.available_balance, 50_000)
        self.assertEqual(self.wallet.frozen_balance, 100_000)

        consume_wallet_payment(
            business_type=allocation.business_type,
            business_order_no=allocation.business_order_no,
        )
        self.wallet.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual(allocation.status, WalletPaymentAllocation.Status.CONSUMED)
        self.assertEqual(self.wallet.available_balance, 50_000)
        self.assertEqual(self.wallet.frozen_balance, 0)

    def test_insufficient_balance_uses_all_balance_and_releases_idempotently(self):
        allocation = prepare_wallet_payment(
            user_id=self.user.pk,
            payable_amount=200_000,
            business_type=WalletPaymentAllocation.BusinessType.ACTIVITY_PUBLISH,
            business_order_no="ACTIVITY-MIXED",
        )
        self.assertEqual(allocation.wallet_amount, 150_000)
        self.assertEqual(allocation.external_amount, 50_000)

        release_wallet_payment(
            business_type=allocation.business_type,
            business_order_no=allocation.business_order_no,
        )
        release_wallet_payment(
            business_type=allocation.business_type,
            business_order_no=allocation.business_order_no,
        )
        self.wallet.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual(allocation.status, WalletPaymentAllocation.Status.RELEASED)
        self.assertEqual(self.wallet.available_balance, 150_000)
        self.assertEqual(self.wallet.frozen_balance, 0)
        self.assertEqual(
            WalletLedgerEntry.objects.filter(
                entry_type=WalletLedgerEntry.EntryType.PAYMENT_RELEASE
            ).count(),
            1,
        )

    def test_partial_refund_returns_recorded_wallet_share_once(self):
        allocation = prepare_wallet_payment(
            user_id=self.user.pk,
            payable_amount=200_000,
            business_type=WalletPaymentAllocation.BusinessType.ACTIVITY_PARTICIPATION,
            business_order_no="ACTIVITY-REFUND",
        )
        consume_wallet_payment(
            business_type=allocation.business_type,
            business_order_no=allocation.business_order_no,
        )
        complete_wallet_refund(
            business_type=allocation.business_type,
            business_order_no=allocation.business_order_no,
            wallet_refund_amount=75_000,
            external_refund_amount=25_000,
            refund_reference_no="REFUND-1",
        )
        complete_wallet_refund(
            business_type=allocation.business_type,
            business_order_no=allocation.business_order_no,
            wallet_refund_amount=75_000,
            external_refund_amount=25_000,
            refund_reference_no="REFUND-1",
        )
        self.wallet.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, 75_000)
        self.assertEqual(allocation.wallet_refunded_amount, 75_000)
        self.assertEqual(allocation.external_refunded_amount, 25_000)
        self.assertEqual(
            allocation.status, WalletPaymentAllocation.Status.PARTIALLY_REFUNDED
        )

    def test_recharge_credit_is_idempotent_and_credits_face_value(self):
        self.wallet.available_balance = 0
        self.wallet.save(update_fields=("available_balance", "updated_at"))
        campaign = RechargeCampaign.objects.create(
            is_enabled=True,
            unit_face_amount=100_000,
            max_quantity_per_order=5,
        )
        RechargeDiscountTier.objects.create(
            campaign=campaign, min_quantity=2, discount_rate_bps=9500
        )
        order = create_recharge_order(user_id=self.user.pk, quantity=2)

        credited, changed = _credit_recharge_order(
            order_no=order.order_no,
            gateway_trade_no="HF-RECHARGE-1",
            paid_at=timezone.now(),
        )
        _, changed_again = _credit_recharge_order(
            order_no=order.order_no,
            gateway_trade_no="HF-RECHARGE-1",
            paid_at=timezone.now(),
        )

        self.wallet.refresh_from_db()
        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(credited.payable_amount, 190_000)
        self.assertEqual(self.wallet.available_balance, 200_000)
        self.assertEqual(
            WalletLedgerEntry.objects.filter(
                entry_type=WalletLedgerEntry.EntryType.RECHARGE
            ).count(),
            1,
        )

    def test_released_allocation_preserves_quote_and_external_refund_is_idempotent(self):
        allocation = prepare_wallet_payment(
            user_id=self.user.pk, payable_amount=200_000,
            business_type="provider_order", business_order_no="RELEASED",
        )
        release_wallet_payment(business_type="provider_order", business_order_no="RELEASED")
        self.wallet.refresh_from_db()
        self.wallet.available_balance += 100_000
        self.wallet.save()
        quote = preview_wallet_payment(
            user_id=self.user.pk, payable_amount=200_000,
            business_type="provider_order", business_order_no="RELEASED",
        )
        self.assertEqual(quote.external_amount, 50_000)
        for _ in range(2):
            complete_wallet_refund(
                business_type="provider_order", business_order_no="RELEASED",
                wallet_refund_amount=0, external_refund_amount=50_000,
                refund_reference_no="EXTERNAL-REFUND",
            )
        allocation.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(allocation.external_refunded_amount, 50_000)
        self.assertEqual(allocation.status, WalletPaymentAllocation.Status.REFUNDED)
        self.assertEqual(self.wallet.available_balance, 250_000)
        with self.assertRaises(ValidationError):
            complete_wallet_refund(
                business_type="provider_order", business_order_no="RELEASED",
                wallet_refund_amount=0, external_refund_amount=1,
                refund_reference_no="OVER-REFUND",
            )

    def test_unpaid_wallet_hold_cannot_be_refunded(self):
        prepare_wallet_payment(
            user_id=self.user.pk, payable_amount=100,
            business_type="provider_order", business_order_no="UNPAID",
        )
        with self.assertRaises(ValidationError):
            complete_wallet_refund(
                business_type="provider_order", business_order_no="UNPAID",
                wallet_refund_amount=100, external_refund_amount=0,
                refund_reference_no="UNPAID-REFUND",
            )

    def test_partial_external_refund_keeps_released_wallet_non_refundable(self):
        prepare_wallet_payment(
            user_id=self.user.pk, payable_amount=200_000,
            business_type="provider_order", business_order_no="PARTIAL-LATE",
        )
        release_wallet_payment(business_type="provider_order", business_order_no="PARTIAL-LATE")
        complete_wallet_refund(
            business_type="provider_order", business_order_no="PARTIAL-LATE",
            wallet_refund_amount=0, external_refund_amount=20_000, refund_reference_no="PARTIAL-LATE-1",
        )
        with self.assertRaises(ValidationError):
            complete_wallet_refund(
                business_type="provider_order", business_order_no="PARTIAL-LATE",
                wallet_refund_amount=1, external_refund_amount=0, refund_reference_no="BAD-LATE",
            )
        allocation = complete_wallet_refund(
            business_type="provider_order", business_order_no="PARTIAL-LATE",
            wallet_refund_amount=0, external_refund_amount=30_000, refund_reference_no="PARTIAL-LATE-2",
        )
        self.assertEqual(allocation.status, WalletPaymentAllocation.Status.REFUNDED)

    def test_wallet_and_pending_recharge_block_account_closure(self):
        from accounts.account_closure import account_closure_blockers

        RechargeCampaign.objects.create(is_enabled=True)
        create_recharge_order(user_id=self.user.pk, quantity=1)
        codes = {item["code"] for item in account_closure_blockers(self.user)}
        self.assertIn("wallet_balance", codes)
        self.assertIn("wallet_recharges", codes)

    def test_expired_unsubmitted_recharge_closes_without_gateway(self):
        RechargeCampaign.objects.create(is_enabled=True)
        order = create_recharge_order(user_id=self.user.pk, quantity=1)
        WalletRechargeOrder.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with patch("wallets.services.get_huifu_payment_gateway") as gateway:
            confirmed, changed = confirm_recharge_payment(order_no=order.order_no)
        self.assertTrue(changed)
        self.assertEqual(confirmed.status, "closed")
        gateway.assert_not_called()

    def test_invalid_admin_inputs_are_validation_errors(self):
        from .serializers import RechargeCampaignInputSerializer, WalletPaginationSerializer

        for data in ({"max_quantity_per_order": "bad"}, {"tiers": [{"min_quantity": None}]}, {"rules_text": "x" * 501}):
            self.assertFalse(RechargeCampaignInputSerializer(data=data).is_valid())
        serializer = RechargeCampaignInputSerializer(data={"is_enabled": "false"})
        self.assertTrue(serializer.is_valid())
        self.assertFalse(serializer.validated_data["is_enabled"])
        self.assertFalse(WalletPaginationSerializer(data={"page": "bad"}).is_valid())

    def test_reconciliation_recovers_a_payment_without_callback(self):
        from .tasks import reconcile_pending_recharges
        from orders.huifu import HuifuPaymentQueryResult

        RechargeCampaign.objects.create(is_enabled=True)
        order = create_recharge_order(user_id=self.user.pk, quantity=1)
        now = timezone.now()
        WalletRechargeOrder.objects.filter(pk=order.pk).update(
            created_at=now - timedelta(minutes=2), req_date="20260926",
            req_seq_id=order.order_no, gateway_merchant_id="test-merchant", trade_type="T_JSAPI",
        )
        query = HuifuPaymentQueryResult(
            req_date="20260926", req_seq_id=order.order_no, huifu_id="test-merchant",
            trans_stat="S", trans_amt="1000.00", end_time="20260926080000",
            trade_type="T_JSAPI", gateway_trade_no="HF-RECOVERED",
            party_order_id="", out_trans_id="", response_code="00000000", response_digest="test",
        )
        with patch("wallets.services.get_huifu_payment_gateway") as gateway:
            gateway.return_value.query_payment.return_value = query
            self.assertEqual(reconcile_pending_recharges(), 1)
            self.assertEqual(reconcile_pending_recharges(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.available_balance, 250_000)

    def test_admin_config_is_audited_and_bad_inputs_do_not_mutate_it(self):
        from backoffice.models import AdminAuditLog

        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        url = "/api/v1/admin/recharge-campaign/"
        good = self.client.patch(url, {
            "is_enabled": True, "tiers": [{"min_quantity": 1, "discount_rate_bps": 9800}],
        }, content_type="application/json")
        self.assertEqual(good.status_code, 200)
        bad = self.client.patch(url, {"tiers": [{"min_quantity": "bad"}]}, content_type="application/json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(RechargeDiscountTier.objects.get().discount_rate_bps, 9800)
        self.assertEqual(AdminAuditLog.objects.filter(action="wallet.recharge_config.update").count(), 1)

    def test_city_role_cannot_read_global_wallets_or_edit_recharge_config(self):
        from backoffice.models import AdminRole, Organization, OrganizationMember

        organization = Organization.objects.create(name="城市运营", code="wallet-city", organization_type=Organization.Type.CITY_AGENT)
        role = AdminRole.objects.create(
            organization=organization, name="城市财务", code="wallet-city-finance",
            data_scope="city", permissions=["wallet.view", "wallet.manage"],
        )
        OrganizationMember.objects.create(user=self.user, organization=organization, role=role)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/v1/admin/wallets/").status_code, 403)
        self.assertEqual(self.client.patch(
            "/api/v1/admin/recharge-campaign/", {"is_enabled": True}, content_type="application/json",
        ).status_code, 403)
        role.permissions = ["*"]
        role.save()
        self.assertEqual(self.client.get("/api/v1/admin/wallets/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/admin/growth/invitations/").status_code, 403)

    def test_permission_migration_does_not_promote_finance_read_only_role(self):
        from importlib import import_module
        from django.apps import apps
        from backoffice.models import AdminRole, Organization

        organization = Organization.objects.create(
            name="平台", code="wallet-platform", organization_type=Organization.Type.PLATFORM,
        )
        role = AdminRole.objects.create(
            organization=organization, name="只读财务", code="wallet-readonly",
            data_scope=AdminRole.DataScope.ALL, permissions=["order.finance.view"],
        )
        migration = import_module("backoffice.migrations.0028_grant_wallet_permissions")
        migration.grant_permissions(apps, None)
        role.refresh_from_db()
        self.assertIn("wallet.view", role.permissions)
        self.assertNotIn("wallet.manage", role.permissions)


class WalletConcurrencyTests(TransactionTestCase):
    def test_concurrent_first_wallet_creation_is_safe(self):
        user = User.objects.create_user(phone="13800009992", password="test-password")
        barrier = Barrier(2)

        def prepare(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return prepare_wallet_payment(
                    user_id=user.pk, business_type="provider_order",
                    business_order_no=f"CONCURRENT-{index}", payable_amount=100,
                ).external_amount
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(prepare, [1, 2]))
        self.assertEqual(results, [100, 100])
        self.assertEqual(UserWallet.objects.filter(user=user).count(), 1)
