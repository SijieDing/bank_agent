import unittest

from authentication import authenticate_card, contains_credentials


class AuthenticationTests(unittest.TestCase):
    def test_three_customers_have_distinct_contexts_without_credentials(self):
        for index, (card, pin) in enumerate([
            ("4111111111111111", "1234"),
            ("5555555555554444", "2345"),
            ("4000000000000002", "3456"),
        ], 1):
            with self.subTest(customer=index):
                self.assertEqual(authenticate_card(card, pin), {
                    "customer_id": f"C1000{index}",
                    "default_account_id": f"CHK-00{index}",
                    "default_card_id": f"VISA-00{index}",
                })
                self.assertIsNone(authenticate_card(card, "0000"))

    def test_card_formatting_and_fresh_context(self):
        context = authenticate_card("4111--1111  1111-1111", "1234")
        self.assertEqual(context["customer_id"], "C10001")
        context["default_account_id"] = "CHK-002"
        self.assertEqual(authenticate_card("4111111111111111", "1234")["default_account_id"], "CHK-001")

    def test_malformed_credentials_fail_closed(self):
        for card, pin in [
            (None, "1234"), (4111111111111111, "1234"),
            ("4111111111111111", 1234), ("4111111111111111", "1234\n"),
            ("4111111111111111", " 1234"), ("4111111111111111", "１２３４"),
            ("unknown", "1234"), ("4111111111111111\n", "1234"),
            ("4" * 65, "1234"), ("4111111111111111", "2345"),
        ]:
            with self.subTest(card=card, pin=pin):
                self.assertIsNone(authenticate_card(card, pin))

    def test_chat_guard_rejects_card_and_pin_text(self):
        for message in [
            "4111111111111111", "My number is 5555-5555-5555-4444",
            "4111--1111  1111-1111", "2026  2026  2026  2026",
            "41  11  11  11  11  11  11  11", "41--11--11--11--11--11--11--11",
            "1111 2222 3333", "PIN: 1234", "my pin number is 2345",
            "My card number is 1111", "2345 is my pin", "3456",
            "Please use 4567 to sign in", "Use 2026 to sign in", "Use 123456", "１２３４", "١٢٣٤",
            "My PIN is 2026", "Pin 1234 dollars", "pin $1234",
        ]:
            with self.subTest(message=message):
                self.assertTrue(contains_credentials(message))

    def test_chat_guard_preserves_normal_banking_dates_and_amounts(self):
        for message in [
            "What is my credit card balance?", "How do I change my PIN?",
            "Show transactions from 2026-07-08 through 2026-10-08",
            "Show transactions on 10/09/2026", "Show 2026 transactions",
            "Show transactions from Oct 7, 2026 through October 9, 2026",
            "Show July 2026 spending", "What did I spend in 2026?", "Did I spend $5000 last month?",
            "Transfer CAD 5000", "Payment of 5000 USD", "I paid 5000 dollars",
            "My balance is 5000.45", "My balance is 5000", "I spent 5000 last month",
            "Search purchases greater than $1,500", "Transfer 1000",
            "Did I spend €1500 or £1800?", "Show my savings transfers",
        ]:
            with self.subTest(message=message):
                self.assertFalse(contains_credentials(message))


if __name__ == "__main__":
    unittest.main()
