from django.test import SimpleTestCase

from .settlement_fees import platform_fee_snapshot


class PlatformFeePolicyTests(SimpleTestCase):
    def test_seventy_thirty_split_keeps_provider_income_before_fees(self):
        snapshot = platform_fee_snapshot(
            provider_amount=7000,
            platform_amount=3000,
            payment_fee_amount=60,
            split_fee_amount=10,
            bank_settlement_fee_amount=20,
        )
        self.assertEqual(snapshot["bearer"], "platform")
        self.assertEqual(snapshot["provider_receivable_amount"], 7000)
        self.assertEqual(snapshot["provider_fee_amount"], 0)
        self.assertEqual(snapshot["total_fee_amount"], 90)
        self.assertEqual(snapshot["platform_net_amount"], 2910)
        self.assertEqual(snapshot["platform_shortfall_amount"], 0)

    def test_unknown_fee_is_not_zero_and_does_not_invent_platform_net(self):
        for unknown in ("payment_fee_amount", "split_fee_amount", "bank_settlement_fee_amount"):
            with self.subTest(unknown=unknown):
                fees = dict(
                    payment_fee_amount=60, split_fee_amount=10, bank_settlement_fee_amount=20
                )
                fees[unknown] = None
                snapshot = platform_fee_snapshot(provider_amount=7000, platform_amount=3000, **fees)
                self.assertIsNone(snapshot["total_fee_amount"])
                self.assertIsNone(snapshot["platform_net_amount"])
                self.assertIsNone(snapshot["platform_shortfall_amount"])
                self.assertEqual(snapshot["provider_receivable_amount"], 7000)

    def test_platform_loss_is_visible_and_not_deducted_from_provider(self):
        for gross in (0, 30):
            with self.subTest(gross=gross):
                snapshot = platform_fee_snapshot(
                    provider_amount=7000,
                    platform_amount=gross,
                    payment_fee_amount=60,
                    split_fee_amount=0,
                    bank_settlement_fee_amount=0,
                )
                self.assertEqual(snapshot["platform_net_amount"], gross - 60)
                self.assertEqual(snapshot["platform_shortfall_amount"], 60 - gross)
                self.assertEqual(snapshot["provider_receivable_amount"], 7000)
                self.assertEqual(snapshot["provider_fee_amount"], 0)

    def test_confirmed_zero_fees_are_distinct_from_unknown_fees(self):
        snapshot = platform_fee_snapshot(
            provider_amount=7000,
            platform_amount=3000,
            payment_fee_amount=0,
            split_fee_amount=0,
            bank_settlement_fee_amount=0,
        )
        self.assertEqual(snapshot["total_fee_amount"], 0)
        self.assertEqual(snapshot["platform_net_amount"], 3000)

    def test_invalid_amounts_are_rejected_without_rounding_or_coercion(self):
        for name in (
            "provider_amount",
            "platform_amount",
            "payment_fee_amount",
            "split_fee_amount",
            "bank_settlement_fee_amount",
        ):
            for value in (-1, True, 0.6, "60"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    amounts = dict(provider_amount=7000, platform_amount=3000)
                    amounts[name] = value
                    platform_fee_snapshot(**amounts)
        with self.assertRaises(ValueError):
            platform_fee_snapshot(provider_amount=None, platform_amount=3000)
