from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.db import OperationalError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from orders.models import ProviderOrder, UserCoupon
from orders.services import (
    build_quote, _settlement_amounts, cancel_provider_order_with_compensation,
    create_provider_order_huifu_payment_session, expire_provider_order_payment,
)
from .lots import CONSUMPTION_PRICING_VERSION, best_discount_rate, best_wallet_discount_rate
from .models import (
    RechargeCampaign, RechargeDiscountTier, UserWallet, WalletBalanceLot,
    WalletRechargeOrder, WalletPaymentAllocation,
)
from .services import (
    _credit_recharge_order, _wallet_for_update, create_recharge_order,
    prepare_wallet_payment, consume_wallet_payment, release_wallet_payment,
    complete_wallet_refund, recharge_order_payload,
)


def credit_lot(user, amount, rate):
    order = WalletRechargeOrder.objects.create(
        user=user, unit_face_amount=amount, quantity=1, credited_amount=amount,
        discount_rate_bps=rate, discount_amount=0, payable_amount=amount,
        pricing_snapshot={"version": CONSUMPTION_PRICING_VERSION},
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    _credit_recharge_order(order_no=order.order_no, gateway_trade_no="", paid_at=timezone.now())
    return WalletBalanceLot.objects.get(recharge_order=order)


class ConsumptionLedgerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="13800009123", password="test")
        # Never allow these ledger tests to contact any external payment service.
        blocker = patch("requests.sessions.Session.send", side_effect=AssertionError("unexpected network"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def allocation(self, amount=9500, name="TEST"):
        return prepare_wallet_payment(user_id=self.user.pk, business_type="provider_order", business_order_no=name, payable_amount=amount)

    def test_credit_face_value_preserves_rate_and_is_idempotent(self):
        campaign = RechargeCampaign.objects.create(is_enabled=True)
        RechargeDiscountTier.objects.create(campaign=campaign, min_quantity=1, discount_rate_bps=9700)
        RechargeDiscountTier.objects.create(campaign=campaign, min_quantity=5, discount_rate_bps=9500)
        order = create_recharge_order(user_id=self.user.pk, quantity=5)
        self.assertEqual((order.credited_amount, order.payable_amount, order.discount_amount), (500000, 500000, 0))
        campaign.discount_tiers.update(discount_rate_bps=9900)
        for _ in range(2):
            _credit_recharge_order(order_no=order.order_no, gateway_trade_no="", paid_at=timezone.now())
        self.assertEqual(WalletBalanceLot.objects.count(), 1)
        self.assertEqual(best_discount_rate(self.user.pk), 9500)
        self.assertEqual(UserWallet.objects.get(user=self.user).available_balance, 500000)
        self.assertEqual(recharge_order_payload(order)["discount_usage"], "consumption")

    def test_legacy_recharge_keeps_original_price_without_extra_benefits(self):
        order = WalletRechargeOrder.objects.create(
            user=self.user, unit_face_amount=500000, quantity=1, credited_amount=500000,
            discount_rate_bps=9500, discount_amount=25000, payable_amount=475000,
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        _credit_recharge_order(order_no=order.order_no, gateway_trade_no="", paid_at=timezone.now())
        self.assertFalse(WalletBalanceLot.objects.exists())
        self.assertEqual(best_discount_rate(self.user.pk), 10000)
        self.assertEqual(recharge_order_payload(order)["payable_amount"], 475000)
        self.assertEqual(recharge_order_payload(order)["discount_usage"], "recharge")

    def test_best_lot_first_then_next_lot_without_external_payment(self):
        worse = credit_lot(self.user, 100000, 9700)
        best = credit_lot(self.user, 5000, 9500)
        allocation = self.allocation()
        self.assertEqual((allocation.wallet_amount, allocation.external_amount), (9500, 0))
        self.assertEqual(list(allocation.lot_debits.values_list("lot_id", "amount")), [(best.pk, 5000), (worse.pk, 4500)])
        self.assertEqual(best_discount_rate(self.user.pk), 9700)
        consume_wallet_payment(business_type="provider_order", business_order_no="TEST")
        consume_wallet_payment(business_type="provider_order", business_order_no="TEST")
        self.assertEqual(UserWallet.objects.get(user=self.user).frozen_balance, 0)

    def test_insufficient_wallet_and_idempotent_release_restores_benefit(self):
        lot = credit_lot(self.user, 5000, 9500)
        allocation = self.allocation()
        self.assertEqual((allocation.wallet_amount, allocation.external_amount), (5000, 4500))
        self.assertEqual(best_discount_rate(self.user.pk), 10000)
        for _ in range(2):
            release_wallet_payment(business_type="provider_order", business_order_no="TEST")
        lot.refresh_from_db()
        self.assertEqual(lot.available_amount, 5000)
        self.assertEqual(best_discount_rate(self.user.pk), 9500)

    def test_same_rate_consumes_oldest_batch_first(self):
        older = credit_lot(self.user, 5000, 9500)
        newer = credit_lot(self.user, 5000, 9500)
        allocation = self.allocation(6000)
        self.assertEqual(
            list(allocation.lot_debits.values_list("lot_id", "amount")),
            [(older.pk, 5000), (newer.pk, 1000)],
        )
        self.assertEqual(best_discount_rate(self.user.pk), 9500)

    def test_wallet_mutations_acquire_owner_before_wallet(self):
        credit_lot(self.user, 5000, 9500)
        with CaptureQueriesContext(connection) as queries:
            _wallet_for_update(self.user.pk)
        selects = [item["sql"] for item in queries if item["sql"].startswith("SELECT")]
        self.assertIn(f'FROM "{User._meta.db_table}"', selects[0])
        self.assertIn(f'FROM "{UserWallet._meta.db_table}"', selects[1])

    def test_partial_refunds_restore_original_lots_without_repricing(self):
        worse = credit_lot(self.user, 10000, 9700)
        best = credit_lot(self.user, 5000, 9500)
        allocation = self.allocation(20000)
        consume_wallet_payment(business_type="provider_order", business_order_no="TEST")
        for _ in range(2):
            complete_wallet_refund(business_type="provider_order", business_order_no="TEST",
                                  wallet_refund_amount=7500, external_refund_amount=2500, refund_reference_no="REFUND-1")
        best.refresh_from_db()
        worse.refresh_from_db()
        self.assertEqual((best.available_amount, worse.available_amount), (5000, 2500))
        complete_wallet_refund(business_type="provider_order", business_order_no="TEST",
                              wallet_refund_amount=7500, external_refund_amount=2500, refund_reference_no="REFUND-2")
        best.refresh_from_db()
        worse.refresh_from_db()
        allocation.refresh_from_db()
        self.assertEqual((best.available_amount, worse.available_amount), (5000, 10000))
        self.assertEqual(allocation.status, "refunded")
        self.assertEqual(UserWallet.objects.get(user=self.user).available_balance, 15000)

    def test_ordinary_balance_used_before_wechat_and_restored_without_benefit(self):
        UserWallet.objects.create(user=self.user, available_balance=2000)
        lot = credit_lot(self.user, 5000, 9500)
        allocation = self.allocation(8000)
        self.assertEqual((allocation.wallet_amount, allocation.external_amount), (7000, 1000))
        consume_wallet_payment(business_type="provider_order", business_order_no="TEST")
        complete_wallet_refund(business_type="provider_order", business_order_no="TEST",
                              wallet_refund_amount=7000, external_refund_amount=1000, refund_reference_no="FULL")
        lot.refresh_from_db()
        self.assertEqual(lot.available_amount, 5000)
        self.assertEqual(UserWallet.objects.get(user=self.user).available_balance, 7000)

    def test_inconsistent_lot_total_fails_closed_and_rolls_back(self):
        lot = credit_lot(self.user, 5000, 9500)
        UserWallet.objects.filter(user=self.user).update(available_balance=1)
        with self.assertRaises(ValidationError):
            self.allocation()
        self.assertFalse(WalletPaymentAllocation.objects.exists())
        lot.refresh_from_db()
        self.assertEqual(lot.available_amount, 5000)


@override_settings(DEBUG=True)
class ConsumptionOrderTests(TestCase):
    # Reuse only the established order fixture, not its entire inherited suite.
    def setUp(self):
        from orders.tests import ProviderOrderApiTests
        ProviderOrderApiTests.setUp(self)
        self.service.price_amount = 5000
        self.service.save(update_fields=("price_amount",))
        blocker = patch("requests.sessions.Session.send", side_effect=AssertionError("unexpected network"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def payload(self):
        from orders.tests import ProviderOrderApiTests
        return ProviderOrderApiTests.payload(self)

    def preview(self, data=None):
        data = data or self.payload()
        response = self.client.post("/api/v1/provider-orders/preview/", data, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return data, response.json()["data"]

    def create(self, data, quote):
        response = self.client.post("/api/v1/provider-orders/", {**data, "pricing_token": quote["pricing_token"]}, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        return ProviderOrder.objects.get(order_no=response.json()["data"]["order_no"])

    def test_coupon_then_whole_order_discount_then_undiscounted_travel(self):
        credit_lot(self.customer, 5000, 9500)
        coupon = UserCoupon.objects.create(owner=self.customer, face_amount=1000, min_order_amount=0, expires_at=timezone.now() + timedelta(days=1))
        data, quote = self.preview({**self.payload(), "coupon_id": str(coupon.public_id)})
        self.assertEqual(quote["service_fee_amount"], 10000)
        self.assertEqual(quote["transport_fee_amount"], 1000)
        self.assertEqual(quote["discount_amount"], 1450)
        self.assertEqual(quote["payable_amount"], 9550)
        order = self.create(data, quote)
        allocation = WalletPaymentAllocation.objects.get(business_order_no=order.order_no)
        self.assertEqual((allocation.wallet_amount, allocation.external_amount), (5000, 4550))
        amounts = _settlement_amounts(order, Decimal("30"))
        self.assertEqual(amounts["net_service_fee_amount"], 8550)
        self.assertEqual(amounts["platform_commission_amount"], 2565)
        self.assertEqual(amounts["provider_settlement_amount"], 6985)

    def test_tiny_best_lot_grants_whole_order_rate_and_next_lot_funds_rest(self):
        credit_lot(self.customer, 20000, 9700)
        credit_lot(self.customer, 1, 9500)
        data, quote = self.preview()
        self.assertEqual(quote["payable_amount"], 10500)
        order = self.create(data, quote)
        allocation = WalletPaymentAllocation.objects.get(business_order_no=order.order_no)
        self.assertEqual((allocation.wallet_amount, allocation.external_amount), (10500, 0))
        result = create_provider_order_huifu_payment_session(order_id=order.pk, customer_id=self.customer.pk, payment_scene="official_account")
        self.assertEqual(result[0].trade_type, "BALANCE")
        order.refresh_from_db()
        self.assertEqual(order.payable_amount, 10500)

    def test_cancel_and_expiry_release_locked_balance_and_coupon(self):
        lot = credit_lot(self.customer, 5000, 9500)
        coupon = UserCoupon.objects.create(
            owner=self.customer, face_amount=1000, min_order_amount=0,
            expires_at=timezone.now() + timedelta(days=1),
        )
        booking = {**self.payload(), "coupon_id": str(coupon.public_id)}
        data, quote = self.preview(booking)
        order = self.create(data, quote)
        cancel_provider_order_with_compensation(order_no=order.order_no, customer_id=self.customer.pk)
        lot.refresh_from_db()
        coupon.refresh_from_db()
        self.assertEqual(lot.available_amount, 5000)
        self.assertEqual(coupon.status, UserCoupon.Status.AVAILABLE)
        data, quote = self.preview(booking)
        order = self.create(data, quote)
        expire_provider_order_payment(order.order_no, now=order.payment_expires_at + timedelta(seconds=1))
        lot.refresh_from_db()
        coupon.refresh_from_db()
        self.assertEqual(lot.available_amount, 5000)
        self.assertEqual(coupon.status, UserCoupon.Status.AVAILABLE)
        self.assertEqual(UserWallet.objects.get(user=self.customer).frozen_balance, 0)

    def test_wallet_api_groups_available_lots_and_excludes_held_amounts(self):
        UserWallet.objects.create(user=self.customer, available_balance=2000)
        credit_lot(self.customer, 3000, 9500)
        credit_lot(self.customer, 2000, 9500)
        credit_lot(self.customer, 10000, 9700)
        response = self.client.get("/api/v1/users/me/wallet/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["discount_balances"], [
            {"discount_rate_bps": 9500, "available_amount": 5000},
            {"discount_rate_bps": 9700, "available_amount": 10000},
        ])
        prepare_wallet_payment(
            user_id=self.customer.pk, business_type="provider_order",
            business_order_no="SUMMARY-HOLD", payable_amount=6000,
        )
        data = self.client.get("/api/v1/users/me/wallet/").json()["data"]
        self.assertEqual(data["discount_balances"], [
            {"discount_rate_bps": 9700, "available_amount": 9000},
        ])
        self.assertEqual(data["ordinary_balance"], 2000)
        self.assertEqual(data["available_balance"], 11000)
        self.assertEqual(data["frozen_balance"], 6000)
        self.assertEqual(data["best_discount_rate_bps"], 9700)

    def test_price_change_since_preview_requires_confirmation(self):
        credit_lot(self.customer, 5000, 9500)
        data, quote = self.preview()
        prepare_wallet_payment(user_id=self.customer.pk, business_type="activity_publish", business_order_no="OTHER", payable_amount=5000)
        response = self.client.post("/api/v1/provider-orders/", {**data, "pricing_token": quote["pricing_token"]}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("pricing_token", response.json())
        self.assertFalse(ProviderOrder.objects.exists())

    def test_missing_or_forged_confirmation_cannot_claim_discount(self):
        credit_lot(self.customer, 5000, 9500)
        for token in ("", "forged"):
            response = self.client.post("/api/v1/provider-orders/", {**self.payload(), "pricing_token": token}, content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletPaymentAllocation.objects.exists())

    def test_expired_confirmation_rejected_without_freezing_money(self):
        credit_lot(self.customer, 5000, 9500)
        with patch("django.core.signing.time.time", return_value=1000000000):
            data, quote = self.preview()
        response = self.client.post("/api/v1/provider-orders/", {**data, "pricing_token": quote["pricing_token"]}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WalletPaymentAllocation.objects.exists())
        self.assertEqual(UserWallet.objects.get(user=self.customer).available_balance, 5000)

    def test_exact_50_wallet_plus_45_external_example(self):
        credit_lot(self.customer, 5000, 9500)
        # Explicitly model an order without travel fees; do not change the
        # production minimum travel fee just to reproduce this pricing example.
        with patch("orders.services.fallback_transport_fee", return_value=0):
            data, quote = self.preview()
            self.assertEqual((quote["payable_amount"], quote["wallet_amount"], quote["external_amount"]), (9500, 5000, 4500))
            order = self.create(data, quote)
        allocation = WalletPaymentAllocation.objects.get(business_order_no=order.order_no)
        self.assertEqual((allocation.wallet_amount, allocation.external_amount), (5000, 4500))

    def test_failed_payment_does_not_lose_reserved_lots_or_mark_paid(self):
        lot = credit_lot(self.customer, 20000, 9500)
        data, quote = self.preview()
        order = self.create(data, quote)
        with patch("taskcenter.services.register_provider_acceptance_timeout", side_effect=RuntimeError("task failure")):
            with self.assertRaises(RuntimeError):
                create_provider_order_huifu_payment_session(order_id=order.pk, customer_id=self.customer.pk, payment_scene="official_account")
        order.refresh_from_db()
        lot.refresh_from_db()
        wallet = UserWallet.objects.get(user=self.customer)
        self.assertEqual(order.status, "pending_payment")
        self.assertEqual((wallet.available_balance, wallet.frozen_balance, lot.available_amount), (9500, 10500, 9500))
        cancel_provider_order_with_compensation(order_no=order.order_no, customer_id=self.customer.pk)
        lot.refresh_from_db()
        self.assertEqual(lot.available_amount, 20000)

    def test_coupon_never_reduces_travel_and_rounding_never_produces_free_payment(self):
        coupon = type("Coupon", (), {"face_amount": 99999, "public_id": "test", "min_order_amount": 0})()
        quote = build_quote(self.service, 120, Decimal("6.8"), coupon=coupon, wallet_discount_rate_bps=9500)
        self.assertEqual(quote.payable_amount, 1000)
        self.assertEqual(quote.snapshot["net_service_fee_amount"], 0)
        quote = build_quote(self.service, 120, None, coupon=coupon, wallet_discount_rate_bps=1)
        self.assertEqual(quote.payable_amount, 1)


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class ConsumptionConcurrencyTests(TransactionTestCase):
    def test_wallet_lock_also_serializes_owner_row(self):
        user = User.objects.create_user(phone="13800009126", password="test")
        credit_lot(user, 5000, 9500)

        def try_owner_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    User.objects.select_for_update(nowait=True).get(pk=user.pk)
                return "acquired"
            except OperationalError as exc:
                return getattr(exc.__cause__, "sqlstate", None) or getattr(exc.__cause__, "pgcode", None)
            finally:
                connection.close()

        with transaction.atomic():
            _wallet_for_update(user.pk)
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(try_owner_lock).result(timeout=10), "55P03")

    def test_duplicate_refund_restores_one_batch_once(self):
        user = User.objects.create_user(phone="13800009125", password="test")
        lot = credit_lot(user, 5000, 9500)
        prepare_wallet_payment(user_id=user.pk, business_type="provider_order", business_order_no="REFUND-RACE", payable_amount=5000)
        consume_wallet_payment(business_type="provider_order", business_order_no="REFUND-RACE")
        barrier = Barrier(2)

        def refund(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                complete_wallet_refund(business_type="provider_order", business_order_no="REFUND-RACE",
                                       wallet_refund_amount=5000, external_refund_amount=0, refund_reference_no="DUPLICATE")
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(refund, [1, 2]))
        lot.refresh_from_db()
        self.assertEqual(lot.available_amount, 5000)
        self.assertEqual(UserWallet.objects.get(user=user).available_balance, 5000)

    def test_best_lot_cannot_be_claimed_by_two_concurrent_orders(self):
        user = User.objects.create_user(phone="13800009124", password="test")
        credit_lot(user, 5000, 9500)
        barrier = Barrier(2)

        def reserve(index):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    wallet = _wallet_for_update(user.pk)
                    rate = best_wallet_discount_rate(wallet)
                    prepare_wallet_payment(user_id=user.pk, business_type="provider_order", business_order_no=f"RACE-{index}", payable_amount=9500 if rate == 9500 else 10000)
                    return rate
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(reserve, [1, 2])), [9500, 10000])
        wallet = UserWallet.objects.get(user=user)
        self.assertEqual((wallet.available_balance, wallet.frozen_balance), (0, 5000))
