import unittest

from gateway import BankingToolGateway


class GatewayIsolationTests(unittest.TestCase):
    def setUp(self):
        self.gateway = BankingToolGateway()

    def test_each_customer_has_distinct_fixture_data(self):
        balances, cards, payments, purchases = [], [], [], []
        for index in range(1, 4):
            account_id, card_id = f"CHK-00{index}", f"VISA-00{index}"
            balances.append(self.gateway.get_balance(account_id)["available_balance"])
            cards.append(self.gateway.get_credit_card_summary(card_id)["current_balance"])
            payments.append(self.gateway.get_upcoming_payments(account_id)["payments"][0]["type"])
            purchases.append(self.gateway.get_transactions(account_id)["total_purchase"])
        for values in (balances, cards, payments, purchases):
            self.assertEqual(len(set(values)), 3)
        self.assertEqual(balances[0], 5320.45)
        self.assertEqual(cards[0], 1842.31)
        self.assertEqual(payments[0], "Mortgage")

    def test_filtering_totals_and_limits_use_selected_account(self):
        recent = self.gateway.get_recent_transactions("CHK-002", limit=1)
        self.assertEqual(recent["transactions"][0]["description"], "Metro")
        self.assertEqual(self.gateway.search_transactions("CHK-002", "costco")["transactions"], [])
        self.assertEqual(self.gateway.search_transactions("CHK-003", "HOME")["transactions"][0]["amount"], 215.40)
        october = self.gateway.get_transactions("CHK-002", "2026-10-01", "2026-10-31")
        self.assertEqual(october["total_saving"], "450.00")
        self.assertEqual(october["total_purchase"], "74.20")
        saving = self.gateway.get_transactions("CHK-003", transaction_type="saving", limit=1)
        self.assertEqual(saving["total_saving"], "800.00")
        self.assertEqual(saving["total_purchase"], "0.00")

    def test_unknown_ids_are_rejected_by_every_method(self):
        account_methods = [
            self.gateway.get_balance, self.gateway.get_recent_transactions,
            self.gateway.search_transactions, self.gateway.get_transactions,
            self.gateway.get_upcoming_payments,
        ]
        for method in account_methods + [self.gateway.get_credit_card_summary]:
            for unknown in ("UNKNOWN", "", None, []):
                with self.subTest(method=method.__name__, unknown=unknown):
                    with self.assertRaises(ValueError):
                        method(unknown)

    def test_contact_information_needs_no_account(self):
        data = self.gateway.get_contact_information()
        self.assertEqual(data["phone"], "+1-800-555-0142")
        self.assertEqual(data["address"], "123 Bank Street, Toronto, ON M5V 1J1")
        self.assertEqual(data["hours"], "Monday to Friday, 9:00 AM to 5:00 PM (Eastern)")

    def test_responses_cannot_mutate_shared_fixtures(self):
        transactions = self.gateway.get_transactions("CHK-002")["transactions"]
        transactions[0]["description"] = "changed"
        transactions.clear()
        self.assertEqual(self.gateway.get_recent_transactions("CHK-002")["transactions"][0]["description"], "Metro")
        payments = self.gateway.get_upcoming_payments("CHK-003")["payments"]
        payments[0]["amount"] = 0
        self.assertEqual(self.gateway.get_upcoming_payments("CHK-003")["payments"][0]["amount"], 425.00)
        card = self.gateway.get_credit_card_summary("VISA-001")
        card["current_balance"] = 0
        self.assertEqual(self.gateway.get_credit_card_summary("VISA-001")["current_balance"], 1842.31)


if __name__ == "__main__":
    unittest.main()
