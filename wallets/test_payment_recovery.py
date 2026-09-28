from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from orders.huifu import HuifuGatewayError, HuifuPaymentQueryResult, HuifuPaymentSessionResult
from orders.payment_recovery import PaymentRecoveryNotice
from orders.wechat_oauth import validate_wechat_payment_payer

from .models import RechargeCampaign, UserWallet, WalletRechargeOrder
from .services import create_recharge_order, create_recharge_huifu_payment_session, confirm_recharge_payment


class RechargePaymentRecoveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone="13800007881", password=None)
        campaign = RechargeCampaign.current()
        campaign.is_enabled = True
        campaign.save()
        self.order = create_recharge_order(user_id=self.user.pk, quantity=1)
        patcher = patch("wallets.services.get_huifu_payment_gateway")
        self.gateway = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.gateway.merchant_id = "test-merchant"
        self.gateway.create_payment.side_effect = HuifuGatewayError(
            response_code="20000000", response_description="重复交易",
        )
        self.gateway.query_payment.return_value = self.query_result("P")

    def query_result(self, state, **overrides):
        values = dict(
            req_date=timezone.localdate().strftime("%Y%m%d"), req_seq_id=self.order.order_no,
            huifu_id="test-merchant", trans_stat=state,
            trans_amt=f"{self.order.payable_amount / 100:.2f}",
            end_time=timezone.localtime().strftime("%Y%m%d%H%M%S"), trade_type="T_JSAPI",
            gateway_trade_no="test-trade", party_order_id="", out_trans_id="",
            response_code="00000000", response_digest="a" * 64,
        )
        values.update(overrides)
        return HuifuPaymentQueryResult(**values)

    def pay(self):
        return create_recharge_huifu_payment_session(
            order_no=self.order.order_no, user_id=self.user.pk,
            payment_scene="official_account", sub_openid="test-payer",
        )

    def assert_notice(self, code):
        with self.assertRaises(PaymentRecoveryNotice) as caught:
            self.pay()
        self.assertEqual(str(caught.exception.detail["code"]), code)

    def test_duplicate_request_queries_without_repeated_create(self):
        self.assert_notice("huifu_payment_pending_confirmation")
        self.assert_notice("huifu_payment_pending_confirmation")
        self.gateway.create_payment.assert_called_once()
        self.assertEqual(self.gateway.query_payment.call_count, 2)
        self.order.refresh_from_db()
        self.assertEqual(self.order.preorder_attempts, 1)

    def test_duplicate_paid_request_credits_once_and_keeps_original_identity(self):
        self.gateway.query_payment.return_value = self.query_result("S")
        self.assert_notice("huifu_payment_status_updated")
        confirm_recharge_payment(order_no=self.order.order_no, user_id=self.user.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WalletRechargeOrder.Status.PAID)
        self.assertEqual(self.order.req_seq_id, self.order.order_no)
        self.assertEqual(UserWallet.objects.get(user=self.user).available_balance, self.order.credited_amount)
        self.gateway.create_payment.assert_called_once()

    def test_terminal_failure_closes_recharge_without_credit(self):
        self.gateway.query_payment.return_value = self.query_result("F")
        self.assert_notice("huifu_payment_closed")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WalletRechargeOrder.Status.CLOSED)
        self.assertFalse(UserWallet.objects.filter(user=self.user, available_balance__gt=0).exists())

    def test_ambiguous_network_failure_and_not_found_never_reposts(self):
        self.gateway.create_payment.side_effect = HuifuGatewayError()
        self.gateway.query_payment.side_effect = HuifuGatewayError(response_code="23000001")
        self.assert_notice("huifu_payment_pending_confirmation")
        self.assert_notice("huifu_payment_pending_confirmation")
        self.gateway.create_payment.assert_called_once()

    def test_stale_submitting_is_queried_not_reposted(self):
        validate_wechat_payment_payer(self.order, trade_type="T_JSAPI", sub_openid="test-payer")
        self.order.req_date = timezone.localdate().strftime("%Y%m%d")
        self.order.req_seq_id = self.order.order_no
        self.order.gateway_merchant_id = "test-merchant"
        self.order.trade_type = "T_JSAPI"
        self.order.payment_scene = "official_account"
        self.order.preorder_status = WalletRechargeOrder.PreorderStatus.SUBMITTING
        self.order.preorder_attempts = 1
        self.order.preorder_requested_at = timezone.now() - timedelta(seconds=31)
        self.order.save()
        self.assert_notice("huifu_payment_pending_confirmation")
        self.gateway.create_payment.assert_not_called()

    def test_late_failure_cannot_overwrite_ready_session(self):
        def late_failure(**kwargs):
            WalletRechargeOrder.objects.filter(pk=self.order.pk).update(
                preorder_status=WalletRechargeOrder.PreorderStatus.READY,
                payment_invoke_payload={"package": "prepay_id=ORIGINAL"},
            )
            raise HuifuGatewayError(response_code="20000000")
        self.gateway.create_payment.side_effect = late_failure
        payment, created = self.pay()
        self.assertFalse(created)
        self.assertEqual(payment.preorder_status, WalletRechargeOrder.PreorderStatus.READY)
        self.assertEqual(payment.payment_invoke_payload, {"package": "prepay_id=ORIGINAL"})

    def test_late_success_cannot_overwrite_newer_attempt(self):
        def late_success(**kwargs):
            WalletRechargeOrder.objects.filter(pk=self.order.pk).update(
                preorder_status=WalletRechargeOrder.PreorderStatus.READY, preorder_attempts=2,
                payment_invoke_payload={"package": "prepay_id=NEWER"},
            )
            return HuifuPaymentSessionResult(
                req_date=kwargs["req_date"], req_seq_id=kwargs["req_seq_id"],
                huifu_id="test-merchant", trade_type="T_JSAPI", trans_stat="P",
                hf_seq_id="test-trade", party_order_id="", out_trans_id="",
                pay_info={"package": "prepay_id=OLD"}, response_code="00000000", response_digest="b" * 64,
            )
        self.gateway.create_payment.side_effect = late_success
        payment, created = self.pay()
        self.assertFalse(created)
        self.assertEqual(payment.payment_invoke_payload, {"package": "prepay_id=NEWER"})

    def test_wrong_amount_cannot_credit_or_recreate(self):
        self.gateway.query_payment.return_value = self.query_result("S", trans_amt="0.01")
        self.assert_notice("huifu_payment_pending_confirmation")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, WalletRechargeOrder.Status.PENDING_PAYMENT)
        self.assertFalse(UserWallet.objects.filter(user=self.user, available_balance__gt=0).exists())

    def test_empty_payment_payload_queries_instead_of_returning_invalid_session(self):
        self.gateway.create_payment.side_effect = None
        self.gateway.create_payment.return_value = HuifuPaymentSessionResult(
            req_date=timezone.localdate().strftime("%Y%m%d"), req_seq_id=self.order.order_no,
            huifu_id="test-merchant", trade_type="T_JSAPI", trans_stat="P",
            hf_seq_id="test-trade", party_order_id="", out_trans_id="",
            pay_info={}, response_code="00000000", response_digest="b" * 64,
        )
        self.assert_notice("huifu_payment_pending_confirmation")
        self.assert_notice("huifu_payment_pending_confirmation")
        self.gateway.create_payment.assert_called_once()

    def test_synchronous_success_still_requires_verified_query_before_credit(self):
        self.gateway.create_payment.side_effect = None
        self.gateway.create_payment.return_value = HuifuPaymentSessionResult(
            req_date=timezone.localdate().strftime("%Y%m%d"), req_seq_id=self.order.order_no,
            huifu_id="test-merchant", trade_type="T_JSAPI", trans_stat="S",
            hf_seq_id="test-trade", party_order_id="", out_trans_id="",
            pay_info={}, response_code="00000000", response_digest="b" * 64,
        )
        self.assert_notice("huifu_payment_pending_confirmation")
        self.assertFalse(UserWallet.objects.filter(user=self.user, available_balance__gt=0).exists())
        self.gateway.query_payment.return_value = self.query_result("S")
        self.assert_notice("huifu_payment_status_updated")
        self.assertEqual(UserWallet.objects.get(user=self.user).available_balance, self.order.credited_amount)
        self.gateway.create_payment.assert_called_once()
