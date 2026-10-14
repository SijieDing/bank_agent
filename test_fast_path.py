import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from bank_fast_path_router import Route, Slots, route_request
from app import BankingAgent
from common_intents import CommonIntentHandler
from gateway import BankingToolGateway
from router import CapabilityRouter, IntentType


def model_client(route: Route) -> Mock:
    client = Mock()
    message = SimpleNamespace(content=json.dumps(route.model_dump()))
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )
    return client


class CapabilityRouterTests(unittest.TestCase):
    def test_validated_model_plan_and_slots_are_returned(self):
        proposal = Route(
            plan_id="balance_after_spending",
            slots=Slots(amount="160"),
            fully_covered=True,
            reason="one hypothetical expense",
        )

        route = CapabilityRouter(
            client=model_client(proposal), model_id="test-model", today=date(2026, 10, 13)
        ).route("If I spend 160 dollars, how much remains?")

        self.assertEqual(route.intent_type, IntentType.COMMON)
        self.assertEqual(route.capability, "balance_after_spending")
        self.assertEqual(route.slots.amount, "160")
        self.assertEqual(route.tools, ("get_balance",))

    def test_unreviewed_plan_falls_back_to_pi(self):
        proposal = Route(
            plan_id="unknown", slots=Slots(), fully_covered=False, reason="unsupported request"
        )

        route = CapabilityRouter(
            client=model_client(proposal), model_id="test-model"
        ).route("Transfer money")

        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.capability, "banking_reasoning")
        self.assertEqual(route.confidence, 0.0)
        self.assertEqual(route.tools, CapabilityRouter.FALLBACK_TOOLS)
        self.assertEqual(route.retry_tools, ())

    def test_invalid_model_claim_is_rejected_by_python_validation(self):
        proposal = Route(
            plan_id="balance_after_spending",
            slots=Slots(amount="999"),
            fully_covered=True,
            reason="expense",
        )

        route = CapabilityRouter(
            client=model_client(proposal), model_id="test-model"
        ).route("If I spend 160 dollars, how much remains?")

        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.tools, ("get_balance",))
        self.assertEqual(route.retry_tools, CapabilityRouter.FALLBACK_TOOLS)
        self.assertIn("does not match", route.reason)

    def test_exact_price_affordability_uses_hypothetical_spending_plan(self):
        proposal = Route(
            plan_id="balance_after_spending",
            slots=Slots(amount="1000"),
            fully_covered=True,
            reason="one exact-price purchase",
        )
        router = CapabilityRouter(client=model_client(proposal), model_id="test-model")

        route = router.route(
            "Do I have enough money to buy a bike which is 1000 dollars?"
        )

        self.assertEqual(route.intent_type, IntentType.COMMON)
        self.assertEqual(route.capability, "balance_after_spending")
        self.assertEqual(route.slots.amount, "1000")
        self.assertEqual(route.tools, ("get_balance",))

    def test_exact_price_affordability_does_not_depend_on_model_generalization(self):
        client = Mock()

        route, _ = route_request(
            client,
            "test-model",
            "Do I have enough money to buy a bike which is 10 dollars?",
        )

        self.assertEqual(route.plan_id, "balance_after_spending")
        self.assertEqual(route.slots.amount, "10")
        client.chat.completions.create.assert_not_called()

    def test_maximum_purchase_price_uses_hypothetical_spending_plan(self):
        client = Mock()

        route, _ = route_request(
            client,
            "test-model",
            "Do I have enough money to buy a bike which is less than 40.6 dollars?",
        )

        self.assertEqual(route.plan_id, "balance_after_spending")
        self.assertEqual(route.slots.amount, "40.6")
        self.assertEqual(route.reason, "one maximum-price purchase")
        client.chat.completions.create.assert_not_called()

    def test_transaction_dates_and_store_are_preserved(self):
        proposal = Route(
            plan_id="query_transactions",
            slots=Slots(
                start_date="2026-10-01", end_date="2026-10-10", store_name="Amazon"
            ),
            fully_covered=True,
            reason="filtered transactions",
        )

        route = CapabilityRouter(
            client=model_client(proposal), model_id="test-model", today=date(2026, 10, 13)
        ).route("Show my transactions about Amazon from 2026-10-01 to 2026-10-10.")

        self.assertEqual(route.capability, "query_transactions")
        self.assertEqual(route.slots.start_date, "2026-10-01")
        self.assertEqual(route.slots.end_date, "2026-10-10")
        self.assertEqual(route.slots.store_name, "Amazon")

    def test_natural_language_transaction_date_range_is_resolved(self):
        proposal = Route(
            plan_id="query_transactions",
            slots=Slots(start_date="2026-10-01", end_date="2026-10-20"),
            fully_covered=True,
            reason="transaction date range",
        )

        route = CapabilityRouter(
            client=model_client(proposal), model_id="test-model", today=date(2026, 10, 13)
        ).route("show me my transactions from 2026 October 1th to 2026 October 20")

        self.assertEqual(route.capability, "query_transactions")
        self.assertEqual(route.slots.start_date, "2026-10-01")
        self.assertEqual(route.slots.end_date, "2026-10-20")

    def test_contact_questions_use_the_public_contact_fast_path(self):
        proposal = Route(
            plan_id="contact_information",
            slots=Slots(),
            fully_covered=True,
            reason="bank contact information",
        )
        router = CapabilityRouter(client=model_client(proposal), model_id="test-model")

        for message in (
            "your contact information",
            "your phone number",
            "your location",
            "Where is your office",
            "Your work hours",
            "When do you open",
        ):
            with self.subTest(message=message):
                route = router.route(message)
                self.assertEqual(route.intent_type, IntentType.COMMON)
                self.assertEqual(route.capability, "contact_information")
                self.assertEqual(route.tools, ("get_contact_information",))

    def test_temporal_opening_question_falls_back_to_pi_with_public_tool(self):
        proposal = Route(
            plan_id="contact_information",
            slots=Slots(),
            fully_covered=True,
            reason="bank opening hours",
        )
        router = CapabilityRouter(client=model_client(proposal), model_id="test-model")

        route = router.route("Do you open tomorrow?")

        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.capability, "banking_reasoning")
        self.assertEqual(route.tools, ("get_contact_information",))

    def test_rejected_close_proposal_uses_only_its_corresponding_tool(self):
        proposal = Route(
            plan_id="contact_information",
            slots=Slots(),
            fully_covered=True,
            reason="bank opening hours",
        )
        router = CapabilityRouter(client=model_client(proposal), model_id="test-model")

        route = router.route("When do you close today?")

        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.tools, ("get_contact_information",))
        self.assertEqual(route.retry_tools, CapabilityRouter.FALLBACK_TOOLS)
        self.assertIn("wording not confirmed", route.reason)

    def test_unavailable_small_model_falls_back_to_pi(self):
        client = Mock()
        client.chat.completions.create.side_effect = OSError("offline")

        route = CapabilityRouter(client=client, model_id="test-model").route("Hello")

        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.tools, CapabilityRouter.FALLBACK_TOOLS)
        self.assertEqual(route.retry_tools, ())
        self.assertIn("unavailable", route.reason)

    def test_invalid_model_output_has_no_proposal_and_uses_full_tool_set(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]
        )

        route = CapabilityRouter(client=client, model_id="test-model").route("Hello")

        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.tools, CapabilityRouter.FALLBACK_TOOLS)
        self.assertEqual(route.retry_tools, ())
        self.assertIn("Model output invalid", route.reason)


class FastPathHandlerTests(unittest.TestCase):
    def setUp(self):
        self.gateway = BankingToolGateway()
        self.handler = CommonIntentHandler(self.gateway)
        self.context = {"default_account_id": "CHK-001"}

    def test_hypothetical_spending_uses_extracted_amount(self):
        answer = self.handler.handle(
            "balance_after_spending", self.context, Slots(amount="160")
        )

        self.assertEqual(
            answer,
            "After spending CAD 160.00, your available balance would be CAD 5,160.45.",
        )

    def test_contact_questions_return_all_contact_information(self):
        answer = self.handler.handle("contact_information", None)

        self.assertEqual(
            answer,
            "Call us at +1-800-555-0142. Our office is at "
            "123 Bank Street, Toronto, ON M5V 1J1, open "
            "Monday to Friday, 9:00 AM to 5:00 PM (Eastern).",
        )

    def test_monthly_projection_uses_extracted_duration(self):
        answer = self.handler.handle(
            "project_balance",
            self.context,
            Slots(amount="20", frequency="monthly", duration_value=2, duration_unit="year"),
        )

        self.assertEqual(
            answer,
            "After 24 monthly expenses of CAD 20.00, your available balance would be CAD 4,840.45.",
        )

    def test_transaction_filters_are_applied_together(self):
        answer = self.handler.handle(
            "query_transactions",
            self.context,
            Slots(
                start_date="2026-10-01",
                minimum_amount="40",
                maximum_amount="100",
                store_name="Amazon",
            ),
        )

        self.assertTrue(
            answer.startswith(
                "The total of your matching transactions is CAD 49.99.\n"
                "Your matching transactions are:\n"
            )
        )
        self.assertIn("Amazon", answer)
        self.assertNotIn("Rogers", answer)

    def test_single_minimum_is_strict_but_a_range_is_inclusive(self):
        threshold = self.gateway.get_transactions("CHK-001", minimum_amount="49.99")
        amount_range = self.gateway.get_transactions(
            "CHK-001", minimum_amount="49.99", maximum_amount="49.99"
        )

        self.assertNotIn("Amazon", [tx["description"] for tx in threshold["transactions"]])
        self.assertEqual(
            [tx["description"] for tx in amount_range["transactions"]], ["Amazon"]
        )

    def test_banking_agent_executes_a_validated_fast_path(self):
        agent = BankingAgent()
        agent._customer_context = {
            "customer_id": "C10001",
            "default_account_id": "CHK-001",
            "default_card_id": "VISA-001",
        }
        agent.router.route = Mock(return_value=SimpleNamespace(
            intent_type=IntentType.COMMON,
            capability="balance_after_spending",
            confidence=1.0,
            tools=("get_balance",),
            slots=Slots(amount="160"),
        ))

        answer = agent.handle("If I spend 160 dollars, how much remains?")

        self.assertIn("CAD 5,160.45", answer)
        self.assertEqual(agent._previous_tools, ("get_balance",))


if __name__ == "__main__":
    unittest.main()
