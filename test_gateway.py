import unittest
from test_support import BankServiceFixture


class GatewayIsolationTests(unittest.TestCase):
    def setUp(self):
        self.service = BankServiceFixture()
        self.addCleanup(self.service.close)
        self.gateway = self.service.gateway()
        self.addCleanup(self.gateway.close)

    def select(self, customer):
        self.gateway.close()
        self.gateway = self.service.gateway(customer)
        self.addCleanup(self.gateway.close)

    def test_each_customer_has_distinct_fixture_data(self):
        balances, cards, payments, purchases = [], [], [], []
        for index in range(1, 4):
            self.select(index)
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
        self.select(2)
        recent = self.gateway.get_recent_transactions("CHK-002", limit=1)
        self.assertEqual(recent["transactions"][0]["description"], "Metro")
        self.assertEqual(self.gateway.search_transactions("CHK-002", "costco")["transactions"], [])
        october = self.gateway.get_transactions("CHK-002", "2026-10-01", "2026-10-31")
        self.assertEqual(october["total_saving"], "450.00")
        self.assertEqual(october["total_purchase"], "74.20")
        self.select(3)
        self.assertEqual(self.gateway.search_transactions("CHK-003", "HOME")["transactions"][0]["amount"], 215.40)
        saving = self.gateway.get_transactions("CHK-003", transaction_type="saving", limit=1)
        self.assertEqual(saving["total_saving"], "800.00")
        self.assertEqual(saving["total_purchase"], "0.00")

    def test_unknown_and_other_customers_ids_are_rejected(self):
        methods = [self.gateway.get_balance, self.gateway.get_recent_transactions,
                   self.gateway.search_transactions, self.gateway.get_transactions,
                   self.gateway.get_upcoming_payments]
        for method in methods:
            for value in ("UNKNOWN", "", None, [], "CHK-002"):
                with self.subTest(method=method.__name__, value=value), self.assertRaises(ValueError):
                    method(value)
        with self.assertRaises(ValueError):
            self.gateway.get_credit_card_summary("VISA-002")

    def test_contact_information_needs_no_account(self):
        public = self.service.gateway(None)
        self.addCleanup(public.close)
        self.assertEqual(public.get_contact_information()["phone"], "+1-800-555-0142")
        with self.assertRaises(PermissionError):
            public.get_balance("CHK-001")

    def test_responses_cannot_mutate_database(self):
        transactions = self.gateway.get_transactions("CHK-001")["transactions"]
        transactions[0]["description"] = "changed"
        self.assertEqual(self.gateway.get_recent_transactions("CHK-001")["transactions"][0]["description"], "Costco")
        payments = self.gateway.get_upcoming_payments("CHK-001")["payments"]
        payments[0]["amount"] = 0
        self.assertEqual(self.gateway.get_upcoming_payments("CHK-001")["payments"][0]["amount"], 2100)
        card = self.gateway.get_credit_card_summary("VISA-001")
        card["current_balance"] = 0
        self.assertEqual(self.gateway.get_credit_card_summary("VISA-001")["current_balance"], 1842.31)
