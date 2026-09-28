from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase
from django.utils import timezone

from .huifu import HuifuGatewayError, safe_gateway_description
from .payment_recovery import (
    PaymentRecoveryNotice, PaymentRecoveryRequired, recoverable_payment_session,
)


class PaymentRecoveryTests(SimpleTestCase):
    def setUp(self):
        self.payment = SimpleNamespace(
            req_date="20260928", req_seq_id="test-request", gateway_merchant_id="test-merchant",
            payment_invoke_payload={}, expires_at=timezone.now() + timedelta(minutes=5),
            refresh_from_db=Mock(), pk=1, preorder_attempts=1,
            _meta=SimpleNamespace(label_lower="test.payment"),
        )
        self.query = Mock(return_value=SimpleNamespace(
            req_date=self.payment.req_date, req_seq_id=self.payment.req_seq_id,
            huifu_id=self.payment.gateway_merchant_id, trans_stat="P",
        ))
        self.apply = Mock()
        self.replay = Mock(return_value="cached")

        @recoverable_payment_session(
            query_payment=self.query, apply_success=self.apply, replay=self.replay,
        )
        def recover():
            raise PaymentRecoveryRequired(self.payment)
        self.recover = recover

    def assert_notice(self, code):
        with self.assertRaises(PaymentRecoveryNotice) as caught:
            self.recover()
        self.assertEqual(str(caught.exception.detail["code"]), code)

    def test_pending_without_payload_cannot_create_or_fake_a_session(self):
        self.assert_notice("huifu_payment_pending_confirmation")
        self.apply.assert_not_called()
        self.replay.assert_not_called()

    def test_pending_replays_existing_valid_payload_only(self):
        self.payment.payment_invoke_payload = {"package": "prepay_id=TEST"}
        self.assertEqual(self.recover(), ("cached", False))
        self.apply.assert_not_called()

    def test_expired_cached_session_is_not_replayed(self):
        self.payment.payment_invoke_payload = {"package": "prepay_id=TEST"}
        self.payment.expires_at = timezone.now() - timedelta(seconds=1)
        self.assert_notice("huifu_payment_pending_confirmation")
        self.replay.assert_not_called()

    def test_paid_uses_existing_success_handler_without_replay(self):
        self.query.return_value.trans_stat = "S"
        self.assert_notice("huifu_payment_status_updated")
        self.apply.assert_called_once_with(self.payment, self.query.return_value)
        self.replay.assert_not_called()

    def test_failed_and_initial_are_not_treated_as_paid(self):
        for state, code in (("F", "huifu_payment_closed"), ("I", "huifu_payment_pending_confirmation")):
            with self.subTest(state=state):
                self.query.return_value.trans_stat = state
                self.assert_notice(code)
        self.apply.assert_not_called()

    def test_not_found_or_query_failure_does_not_permit_replacement_charge(self):
        for code in ("23000001", "20000004", "", "99999999"):
            with self.subTest(code=code):
                self.query.side_effect = HuifuGatewayError(response_code=code)
                self.assert_notice("huifu_payment_pending_confirmation")
        self.replay.assert_not_called()

    def test_query_identity_mismatch_cannot_apply_success(self):
        self.query.return_value.trans_stat = "S"
        self.query.return_value.req_seq_id = "another-order"
        with self.assertRaises(HuifuGatewayError):
            self.recover()
        self.apply.assert_not_called()

    def test_diagnostics_remove_identifiers_and_secrets(self):
        description = safe_gateway_description(
            'fee_flag 未开通权限 13800138000 openid=oSensitiveToken appid=wxSecret '
            'https://example.test/?secret=supersecret "姓名张三"\n'
        )
        self.assertIn("fee_flag 未开通权限", description)
        for secret in ("13800138000", "oSensitiveToken", "wxSecret", "supersecret", "张三", "\n"):
            self.assertNotIn(secret, description)
        self.assertEqual(safe_gateway_description("-----BEGIN PRIVATE KEY-----key"), "[密钥内容已隐藏]")
        self.assertEqual(safe_gateway_description({"secret": "value"}), "")

