from unittest.mock import patch

from django.test import SimpleTestCase

from .huifu import HuifuAggregatePaymentGateway, HuifuPaymentConfig


class HuifuAggregatePaymentGatewayTests(SimpleTestCase):
    def setUp(self):
        self.gateway = HuifuAggregatePaymentGateway(
            HuifuPaymentConfig(
                enabled=True,
                environment="mertest",
                sys_id="6666000000000001",
                product_id="YYZY",
                merchant_id="6666000000000002",
                private_key="private-key",
                public_key="public-key",
                skill_source="hfps/1.3.5",
                notify_url="https://api.example.test/api/v1/payments/huifu/notify/",
                wechat_official_account_app_id="wx-app-id",
                wechat_mobile_app_id="wx-mobile-id",
                fee_flag="1",
                connect_timeout_seconds=15,
            )
        )

    @patch("dg_sdk.Payment.refund_query")
    @patch("dg_sdk.Payment.refund")
    def test_refund_and_query_keep_separate_request_identities(
        self,
        refund_call,
        query_call,
    ):
        refund_call.return_value = {
            "data": {
                "resp_code": "00000100",
                "huifu_id": self.gateway.merchant_id,
                "req_date": "20260919",
                "req_seq_id": "POR-REFUND-001",
                "ord_amt": "36.60",
                "hf_seq_id": "HF-REFUND-001",
                "trans_stat": "P",
            }
        }
        query_call.return_value = {
            "data": {
                "resp_code": "00000000",
                "huifu_id": self.gateway.merchant_id,
                "org_req_date": "20260919",
                "org_req_seq_id": "POR-REFUND-001",
                "org_hf_seq_id": "HF-REFUND-001",
                "ord_amt": "36.60",
                "actual_ref_amt": "36.60",
                "trans_stat": "S",
                "trans_finish_time": "20260919153000",
            }
        }

        accepted = self.gateway.refund_payment(
            req_date="20260919",
            req_seq_id="POR-REFUND-001",
            amount=3660,
            org_req_date="20260918",
            org_req_seq_id="POP-PAYMENT-001",
            org_hf_seq_id="HF-PAYMENT-001",
            remark="用户取消",
        )
        confirmed = self.gateway.query_refund(
            req_date="20260919",
            req_seq_id="POR-REFUND-001",
            refund_hf_seq_id="HF-REFUND-001",
        )

        refund_request = refund_call.call_args.args[0].combileParams()
        self.assertEqual(refund_request["req_seq_id"], "POR-REFUND-001")
        self.assertEqual(refund_request["org_hf_seq_id"], "HF-PAYMENT-001")
        self.assertNotIn("org_req_seq_id", refund_request)
        self.assertEqual(refund_request["notify_url"], self.gateway.config.notify_url)
        query_request = query_call.call_args.args[0].combileParams()
        self.assertEqual(query_request["org_hf_seq_id"], "HF-REFUND-001")
        self.assertEqual(query_request["org_req_date"], "20260919")
        self.assertEqual(accepted.trans_stat, "P")
        self.assertEqual(confirmed.trans_stat, "S")
        self.assertEqual(confirmed.req_seq_id, "POR-REFUND-001")

    @patch("dg_sdk.Payment.close_query")
    @patch("dg_sdk.Payment.close")
    def test_close_and_close_query_use_independent_idempotency_keys(
        self,
        close_call,
        close_query_call,
    ):
        close_call.return_value = {
            "data": {
                "resp_code": "00000100",
                "huifu_id": self.gateway.merchant_id,
                "req_date": "20260919",
                "req_seq_id": "POP-001-CLOSE",
                "org_trans_stat": "P",
                "trans_stat": "P",
            }
        }
        close_query_call.return_value = {
            "data": {
                "resp_code": "00000000",
                "huifu_id": self.gateway.merchant_id,
                "req_date": "20260919",
                "req_seq_id": "POP-001-CLOSEQ",
                "org_trans_stat": "F",
                "trans_stat": "S",
            }
        }

        accepted = self.gateway.close_payment(
            req_date="20260919",
            req_seq_id="POP-001-CLOSE",
            org_req_date="20260918",
            org_req_seq_id="POP-001",
        )
        confirmed = self.gateway.query_close(
            req_date="20260919",
            req_seq_id="POP-001-CLOSEQ",
            org_req_date="20260918",
            org_req_seq_id="POP-001",
        )

        self.assertEqual(
            close_call.call_args.args[0].combileParams()["org_req_seq_id"],
            "POP-001",
        )
        self.assertEqual(
            close_query_call.call_args.args[0].combileParams()["req_seq_id"],
            "POP-001-CLOSEQ",
        )
        self.assertEqual(accepted.trans_stat, "P")
        self.assertEqual(confirmed.trans_stat, "S")
