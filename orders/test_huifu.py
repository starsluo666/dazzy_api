import base64
import json
from dataclasses import replace
from unittest.mock import patch

from Crypto.PublicKey import RSA
from dg_sdk import Payment
from dg_sdk.core.rsa_utils import rsa_sign
from django.test import SimpleTestCase

from .huifu import HuifuAggregatePaymentGateway, HuifuPaymentConfig


class HuifuAggregatePaymentGatewayTests(SimpleTestCase):
    def config(self):
        return HuifuPaymentConfig(
            enabled=True,
            environment="mertest",
            sys_id="sys-id",
            product_id="product-id",
            merchant_id="6666000000000000",
            private_key="private-key",
            public_key="public-key",
            skill_source="hfps/1.3.5",
            notify_url="https://api.example.test/api/v1/payments/huifu/notify/",
            wechat_official_account_app_id="wx-official-app-id",
            wechat_mobile_app_id="wx-mobile-app-id",
            fee_flag="1",
            connect_timeout_seconds=15,
        )

    def test_jsapi_payment_uses_official_sdk_and_stringifies_method_expand(self):
        response = {
            "resp_code": "00000000",
            "req_date": "20260916",
            "req_seq_id": "POP123",
            "huifu_id": "6666000000000000",
            "trade_type": "T_JSAPI",
            "trans_stat": "P",
            "hf_seq_id": "HF-GLOBAL-001",
            "party_order_id": "PARTY-001",
            "pay_info": json.dumps(
                {
                    "appId": "wx-official-app-id",
                    "timeStamp": "1660000000",
                    "nonceStr": "nonce",
                    "package": "prepay_id=PREPAY",
                    "signType": "RSA",
                    "paySign": "signature",
                }
            ),
        }
        with patch.object(Payment, "create", return_value=response) as sdk_create:
            result = HuifuAggregatePaymentGateway(self.config()).create_payment(
                req_date="20260916",
                req_seq_id="POP123",
                amount=36600,
                goods_desc="旅游陪伴",
                trade_type="T_JSAPI",
                attach="DZY-ORDER-001",
                time_expire="20260916235959",
                sub_openid="user-openid",
            )

        request = sdk_create.call_args.args[0]
        self.assertEqual(request.req_date, "20260916")
        self.assertEqual(request.req_seq_id, "POP123")
        self.assertEqual(request.huifu_id, "6666000000000000")
        self.assertEqual(request.trade_type, "T_JSAPI")
        self.assertEqual(request.trans_amt, "366.00")
        self.assertEqual(request.fee_flag, "1")
        self.assertEqual(request.notify_url, self.config().notify_url)
        method_expand = json.loads(request.method_expand)
        self.assertEqual(method_expand["sub_appid"], "wx-official-app-id")
        self.assertEqual(method_expand["sub_openid"], "user-openid")
        self.assertEqual(method_expand["attach"], "DZY-ORDER-001")
        self.assertEqual(result.trade_type, "T_JSAPI")
        self.assertEqual(result.hf_seq_id, "HF-GLOBAL-001")
        self.assertEqual(result.pay_info["package"], "prepay_id=PREPAY")

    def test_payment_query_uses_original_request_identity(self):
        response = {
            "resp_code": "00000000",
            "req_date": "20260915",
            "req_seq_id": "POP123",
            "huifu_id": "6666000000000000",
            "hf_seq_id": "HF-GLOBAL-001",
            "party_order_id": "PARTY-001",
            "trans_stat": "S",
            "trans_amt": "366.00",
            "end_time": "20260916120000",
            "trade_type": "T_JSAPI",
        }
        with patch.object(Payment, "query", return_value=response) as sdk_query:
            result = HuifuAggregatePaymentGateway(self.config()).query_payment(
                req_date="20260915",
                req_seq_id="POP123",
                hf_seq_id="HF-GLOBAL-001",
            )

        request = sdk_query.call_args.args[0]
        self.assertEqual(request.req_date, "20260915")
        self.assertEqual(request.hf_seq_id, "HF-GLOBAL-001")
        self.assertEqual(request.req_seq_id, "")
        self.assertEqual(result.trans_stat, "S")
        self.assertEqual(result.gateway_trade_no, "HF-GLOBAL-001")
        self.assertEqual(result.trade_type, "T_JSAPI")

    def test_notification_signature_verifies_the_original_resp_data_string(self):
        key = RSA.generate(1024)
        private_key = base64.b64encode(key.export_key(format="DER", pkcs=8)).decode("ascii")
        public_key = base64.b64encode(key.public_key().export_key(format="DER")).decode("ascii")
        resp_data = '{"trans_amt":"1.00","trans_stat":"S"}'
        signed, signature = rsa_sign(private_key, resp_data)
        self.assertTrue(signed)
        gateway = HuifuAggregatePaymentGateway(replace(self.config(), public_key=public_key))

        self.assertTrue(
            gateway.verify_payment_notification(resp_data=resp_data, sign=signature)
        )
        self.assertFalse(
            gateway.verify_payment_notification(
                resp_data='{"trans_amt":"1.0","trans_stat":"S"}',
                sign=signature,
            )
        )
