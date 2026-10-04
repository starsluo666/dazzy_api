"""Synthetic query results only; no channel requests or financial operations."""

import json
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from .cash_accounts import verify_cash_configuration
from .test_receiving_accounts import TEST_KEY


@override_settings(PROVIDER_RECEIVING_ACCOUNT_ENCRYPTION_KEY=TEST_KEY)
class CashConfigurationTests(SimpleTestCase):
    def setUp(self):
        self.details = {
            "real_name": "测试达人", "bank_card_number": "6222000000000000",
            "bank_province_code": "130000", "bank_city_code": "130400",
        }
        self.expected = {
            "cash_type": "T1", "fix_amt": "0.00", "out_fee_flag": "1",
            "out_fee_huifu_id": "9000000000000004", "out_fee_acct_type": "01",
        }
        self.cash = {
            "cash_type": "T1", "fix_amt": "0.00", "switch_state": "1",
            "out_cash_flag": "1", "out_cash_huifuid": "9000000000000004",
            "out_cash_acct_type": "01",
        }
        self.card = {
            "card_type": "1", "card_name": self.details["real_name"],
            "card_no": self.details["bank_card_number"], "prov_id": "130000",
            "area_id": "130400", "status": "N", "token_no": "TESTTOKEN1",
        }
        self.response = {
            "settle_config_list": "[]",
            "qry_cash_config_list": json.dumps([self.cash]),
            "qry_cash_card_info_list": json.dumps([self.card]),
        }

    def verify(self, response=None, expected=None):
        # Every call starts with stale success: the query must overwrite each group.
        self.account = SimpleNamespace(
            provider_id=1, card_status="S", cash_status="S",
            automatic_settlement_disabled=True, verified_cash_config=self.expected,
            cash_card_ciphertext="stale-token",
        )
        ready = verify_cash_configuration(
            self.account, self.response if response is None else response, self.details,
            self.expected if expected is None else expected,
        )
        self.assertLessEqual(len(self.account.channel_message), 200)
        for private in (*self.details.values(), "TESTTOKEN1", self.expected["out_fee_huifu_id"]):
            self.assertNotIn(private, self.account.channel_message)
        return ready

    def test_complete_result_and_explicit_empty_settlements_are_ready(self):
        self.assertTrue(self.verify())
        self.assertTrue(self.account.automatic_settlement_disabled)
        self.assertEqual(self.account.verified_cash_config, self.expected)
        self.assertNotEqual(self.account.cash_card_ciphertext, "stale-token")
        self.assertEqual(self.account.channel_message, "")
        self.response["settle_config_list"] = '[{"settle_status":"0"}]'
        self.assertTrue(self.verify())

    def test_missing_malformed_or_native_groups_stay_unknown_independently(self):
        fields = {
            "settle_config_list": "automatic_settlement_disabled",
            "qry_cash_config_list": "cash_status",
            "qry_cash_card_info_list": "card_status",
        }
        for key, state in fields.items():
            for value in (None, "", "not-json", "{}", "null", [], [{}], "[null]", "[1]"):
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.verify({**self.response, key: value}))
                    self.assertEqual(getattr(self.account, state), None if state == "automatic_settlement_disabled" else "")
                    for other in set(fields.values()) - {state}:
                        self.assertEqual(getattr(self.account, other), True if other == "automatic_settlement_disabled" else "S")
                    self.assertIn("未返回" if value in (None, "") else "格式异常", self.account.channel_message)
                    if state == "card_status":
                        self.assertEqual(self.account.cash_card_ciphertext, "")
                    elif state == "cash_status":
                        self.assertEqual(self.account.verified_cash_config, {})
            response = dict(self.response)
            response.pop(key)
            self.assertFalse(self.verify(response))
            self.assertIn("未返回", self.account.channel_message)

    def test_missing_settlement_status_is_not_reported_as_enabled(self):
        for status in (None, "", "unknown", 0, False, []):
            self.response["settle_config_list"] = json.dumps([{"settle_status": status}])
            self.assertFalse(self.verify())
            self.assertIsNone(self.account.automatic_settlement_disabled)
        self.response["settle_config_list"] = '[{"settle_status":"1"}]'
        self.assertFalse(self.verify())
        self.assertIs(self.account.automatic_settlement_disabled, False)
        self.assertIn("仍开启", self.account.channel_message)

    def test_cash_absence_closed_or_mismatch_is_not_unknown(self):
        rows = ([], [{**self.cash, "switch_state": "0"}],
                [{**self.cash, "fix_amt": "1.00"}],
                [{**self.cash, "out_cash_huifuid": "9000000000000009"}])
        for cash in rows:
            self.response["qry_cash_config_list"] = json.dumps(cash)
            self.assertFalse(self.verify())
            self.assertEqual(self.account.cash_status, "F")
            self.assertEqual(self.account.verified_cash_config, {})

    def test_missing_cash_fields_and_duplicate_cycles_stay_unknown(self):
        for key in self.cash:
            cash = dict(self.cash)
            cash.pop(key)
            self.response["qry_cash_config_list"] = json.dumps([cash])
            self.assertFalse(self.verify())
            self.assertEqual(self.account.cash_status, "")
            self.assertIn("不完整", self.account.channel_message)
        self.response["qry_cash_config_list"] = json.dumps([self.cash, self.cash])
        self.assertFalse(self.verify())
        self.assertEqual(self.account.cash_status, "")

    def test_cash_snapshot_absent_or_legacy_is_not_a_channel_failure(self):
        for expected in ({}, {"settle_cycle": "T1"}, {"cash_type": "T1"}, []):
            self.assertFalse(self.verify(expected=expected))
            self.assertEqual(self.account.cash_status, "")
            self.assertEqual(self.account.verified_cash_config, {})
            self.assertIn("授权配置", self.account.channel_message)

    def test_missing_masked_or_ambiguous_card_is_unknown(self):
        for key in self.card:
            card = dict(self.card)
            card.pop(key)
            self.response["qry_cash_card_info_list"] = json.dumps([card])
            self.assertFalse(self.verify())
            self.assertEqual(self.account.card_status, "")
            self.assertEqual(self.account.cash_card_ciphertext, "")
        for cards in ([{**self.card, "card_no": "6222********0000"}],
                      [self.card, self.card], [{**self.card, "token_no": "bad-token"}]):
            self.response["qry_cash_card_info_list"] = json.dumps(cards)
            self.assertFalse(self.verify())
            self.assertEqual(self.account.card_status, "")
            self.assertEqual(self.account.cash_card_ciphertext, "")

    def test_no_matching_normal_own_card_is_not_unknown(self):
        for cards in ([], [{**self.card, "status": "C"}],
                      [{**self.card, "card_no": "6222000000009999"}]):
            self.response["qry_cash_card_info_list"] = json.dumps(cards)
            self.assertFalse(self.verify())
            self.assertEqual(self.account.card_status, "F")
            self.assertEqual(self.account.cash_card_ciphertext, "")

    def test_multiple_issues_are_reported_without_raw_values(self):
        response = {key: 'sensitive-invalid-json' for key in self.response}
        self.assertFalse(self.verify(response, expected={}))
        self.assertEqual(self.account.channel_message.count("格式异常"), 3)
        self.assertNotIn("sensitive-invalid-json", self.account.channel_message)
