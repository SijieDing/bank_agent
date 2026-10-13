import io
import unittest
from unittest.mock import call, patch

from app import BankingAgent, main
from pi_agent import PiReply
from router import IntentType, RouteResult


CUSTOMER_CONTEXT = {
    "customer_id": "C10001",
    "default_account_id": "CHK-001",
    "default_card_id": "VISA-001",
}
DEMO_CREDENTIALS = (
    ("4111111111111111", "1234"),
    ("5555555555554444", "2345"),
    ("4000000000000002", "3456"),
)


class BankingAgentTestCase(unittest.TestCase):
    def setUp(self):
        self.agent = BankingAgent()
        self.pi_patch = patch.object(self.agent.pi_agent, "handle", return_value=PiReply("Pi answer"))
        self.pi_handle = self.pi_patch.start()
        self.addCleanup(self.pi_patch.stop)
        self.common_patch = patch.object(
            self.agent.common_handler, "handle", return_value="Verified common answer"
        )
        self.common_handle = self.common_patch.start()
        self.addCleanup(self.common_patch.stop)
        self.output_patch = patch("sys.stdout", new_callable=io.StringIO)
        self.output_patch.start()
        self.addCleanup(self.output_patch.stop)

    def request_authentication(self):
        self.pi_handle.return_value = PiReply("Please sign in", True)
        self.agent.handle("Please sign me in")
        self.pi_handle.return_value = PiReply("Pi answer")

    def authenticate(self):
        self.request_authentication()
        self.assertTrue(self.agent.authenticate(*DEMO_CREDENTIALS[0]))
        self.agent.reset()
        self.pi_handle.reset_mock()


class AuthenticationTests(BankingAgentTestCase):
    def test_unauthenticated_requests_use_only_llm_decision(self):
        questions = ["Hello", "What is my balance?", "Show my recent transactions"]
        for index, question in enumerate(questions):
            self.assertEqual(self.agent.handle(question), "Pi answer")
            self.assertEqual(self.pi_handle.call_args, call(
                question, None, (),
                conversation_history=[
                    {"user": previous, "assistant": "Pi answer"}
                    for previous in questions[:index]
                ],
            ))

        self.assertIsNone(self.agent.customer_context)
        self.assertFalse(self.agent.authentication_requested)
        self.common_handle.assert_not_called()

    def test_authentication_requires_llm_request(self):
        with self.assertRaises(ValueError):
            self.agent.authenticate(*DEMO_CREDENTIALS[0])
        self.assertEqual(self.agent.failed_auth_attempts, 0)
        self.assertIsNone(self.agent.customer_context)

        self.agent.handle("What is my balance?")
        with self.assertRaises(ValueError):
            self.agent.authenticate(*DEMO_CREDENTIALS[0])

        self.request_authentication()
        self.assertTrue(self.agent.authentication_requested)
        self.assertTrue(self.agent.authenticate(*DEMO_CREDENTIALS[0]))
        self.assertFalse(self.agent.authentication_requested)
        self.assertEqual(self.agent.customer_context, CUSTOMER_CONTEXT)

    def test_each_of_three_users_authenticates_to_distinct_accounts(self):
        contexts = []
        for credentials in DEMO_CREDENTIALS:
            with self.subTest(card=credentials[0]):
                agent = BankingAgent()
                with patch.object(agent.pi_agent, "handle", return_value=PiReply("Sign in", True)):
                    agent.handle("What is my balance?")
                self.assertTrue(agent.authenticate(*credentials))
                self.assertEqual(agent.failed_auth_attempts, 0)
                contexts.append(agent.customer_context)

        for key in CUSTOMER_CONTEXT:
            self.assertEqual(len({context[key] for context in contexts}), 3)

    def test_card_and_pin_must_belong_to_same_user(self):
        for index, (card_number, pin) in enumerate(DEMO_CREDENTIALS):
            with self.subTest(card=card_number):
                agent = BankingAgent()
                with patch.object(agent.pi_agent, "handle", return_value=PiReply("Sign in", True)):
                    agent.handle("What is my balance?")
                other_pin = DEMO_CREDENTIALS[(index + 1) % 3][1]
                self.assertFalse(agent.authenticate(card_number, other_pin))
                self.assertIsNone(agent.customer_context)
                self.assertEqual(agent.failed_auth_attempts, 1)
                self.assertTrue(agent.authenticate(card_number, pin))

    def test_credentials_never_enter_model_or_conversation_history(self):
        self.request_authentication()
        model_calls = self.pi_handle.call_args_list.copy()
        history = self.agent.history.copy()

        self.assertFalse(self.agent.authenticate(DEMO_CREDENTIALS[0][0], "9999"))
        self.assertTrue(self.agent.authenticate(*DEMO_CREDENTIALS[0]))

        self.assertEqual(self.pi_handle.call_args_list, model_calls)
        self.assertEqual(self.agent.history, history)
        self.common_handle.assert_not_called()
        for credential in (*DEMO_CREDENTIALS[0], "9999"):
            self.assertNotIn(credential, str(self.pi_handle.call_args_list))
            self.assertNotIn(credential, str(self.agent.history))
        self.assertEqual(self.agent.failed_auth_attempts, 1)

    def test_authenticated_identity_cannot_be_switched_or_mutated(self):
        self.authenticate()
        returned_context = self.agent.customer_context
        for key in returned_context:
            returned_context[key] = "another-customer"
        with self.assertRaises(ValueError):
            self.agent.authenticate(*DEMO_CREDENTIALS[1])
        with self.assertRaises(TypeError):
            self.agent.handle("What is my balance?", returned_context)

        self.agent.handle("Switch to the other customer's balance")
        self.agent.reset()
        self.agent.handle("What is my balance?")

        self.assertEqual(self.agent.customer_context, CUSTOMER_CONTEXT)
        self.common_handle.assert_called_with("account_balance", CUSTOMER_CONTEXT)
        self.assertFalse(self.agent.authentication_requested)

    def test_third_failure_closes_conversation_and_reset_cannot_reopen_it(self):
        self.request_authentication()
        self.assertEqual(self.agent.MAX_AUTH_ATTEMPTS, 3)
        for attempt in range(1, 4):
            self.assertFalse(self.agent.authenticate(DEMO_CREDENTIALS[0][0], "9999"))
            self.assertEqual(self.agent.failed_auth_attempts, attempt)
            self.assertEqual(self.agent.closed, attempt == 3)

        calls = self.pi_handle.call_count
        self.agent.reset()
        self.assertTrue(self.agent.closed)
        self.assertEqual(self.agent.failed_auth_attempts, 3)
        self.assertIsNone(self.agent.customer_context)
        with self.assertRaises((ValueError, RuntimeError)):
            self.agent.authenticate(*DEMO_CREDENTIALS[0])
        with self.assertRaises((ValueError, RuntimeError)):
            self.agent.handle("What is my balance?")
        self.assertEqual(self.pi_handle.call_count, calls)
        self.common_handle.assert_not_called()

    def test_reset_preserves_pending_authentication_and_failure_count(self):
        self.request_authentication()
        self.assertFalse(self.agent.authenticate(DEMO_CREDENTIALS[0][0], "9999"))
        self.agent.reset()

        self.assertEqual(self.agent.history, [])
        self.assertTrue(self.agent.authentication_requested)
        self.assertEqual(self.agent.failed_auth_attempts, 1)
        self.assertTrue(self.agent.authenticate(*DEMO_CREDENTIALS[0]))
        self.agent.reset()
        self.assertEqual(self.agent.customer_context, CUSTOMER_CONTEXT)
        self.assertEqual(self.agent.failed_auth_attempts, 1)

    def test_contact_information_needs_no_sign_in_or_pi(self):
        with patch.object(self.agent.router, "route") as route:
            route.return_value = RouteResult(
                intent_type=IntentType.COMMON,
                capability="contact_information",
                confidence=0.95,
            )
            answer = self.agent.handle("What is your phone number?")
        self.assertEqual(answer, "Verified common answer")
        self.pi_handle.assert_not_called()
        self.common_handle.assert_called_once_with("contact_information", None)
        self.assertIsNone(self.agent.customer_context)
        self.assertFalse(self.agent.authentication_requested)

    def test_contact_information_uses_no_tools_and_context(self):
        self.authenticate()
        with patch.object(self.agent.router, "route") as route:
            route.return_value = RouteResult(
                intent_type=IntentType.COMMON,
                capability="contact_information",
                confidence=0.95,
            )
            answer = self.agent.handle("Where is your office?")
        self.assertEqual(answer, "Verified common answer")
        self.pi_handle.assert_not_called()
        self.common_handle.assert_called_once_with("contact_information", None)

    def test_location_wording_uses_contact_fast_path_without_sign_in(self):
        question = "Where is your location?"

        answer = self.agent.handle(question)

        self.assertEqual(answer, "Verified common answer")
        self.pi_handle.assert_not_called()
        self.common_handle.assert_called_once_with("contact_information", None)
        self.assertFalse(self.agent.authentication_requested)
        self.assertIsNone(self.agent.customer_context)

    def test_credentials_in_chat_are_rejected_before_routing_or_model(self):
        messages = (
            "4111111111111111", "My card is 4111 1111 1111 1111",
            "My PIN is 1234", "1234",
        )
        for authenticated in (False, True):
            if authenticated:
                self.authenticate()
            with patch.object(self.agent.router, "route") as route:
                for message in messages:
                    with self.subTest(authenticated=authenticated, message=message):
                        with self.assertRaises(ValueError):
                            self.agent.handle(message)
                route.assert_not_called()
            self.pi_handle.assert_not_called()
            self.common_handle.assert_not_called()
            self.assertEqual(self.agent.history, [])


class ConversationTests(BankingAgentTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_merchant_and_period_followups_keep_history_and_tools(self):
        questions = [
            "How much did I spend at Costco in the last 3 months?",
            "What about Amazon?",
            "Last month only",
        ]
        for question in questions:
            self.agent.handle(question)

        for index, recorded in enumerate(self.pi_handle.call_args_list):
            with self.subTest(index=index):
                self.assertEqual(recorded.args, (
                    questions[index], CUSTOMER_CONTEXT, ("get_transactions",)
                ))
                self.assertEqual(recorded.kwargs["conversation_history"], [
                    {"user": question, "assistant": "Pi answer"}
                    for question in questions[:index]
                ])

    def test_common_answers_supply_context_and_relevant_tools(self):
        cases = [
            ("What is my balance?", "get_balance"),
            ("Show my recent transactions", "get_transactions"),
            ("What is my credit card balance?", "get_credit_card_summary"),
        ]
        for question, tool in cases:
            with self.subTest(question=question):
                self.agent.reset()
                self.pi_handle.reset_mock()
                self.agent.handle(question)
                self.agent.handle("Explain that amount")

                self.pi_handle.assert_called_once_with(
                    "Explain that amount", CUSTOMER_CONTEXT, (tool,),
                    conversation_history=[{
                        "user": question, "assistant": "Verified common answer"
                    }],
                )

    def test_reset_clears_history_and_inherited_tools_but_keeps_identity(self):
        self.agent.handle("How much did I spend last month?")
        self.agent.reset()
        self.assertEqual(self.agent.history, [])
        self.assertEqual(self.agent.customer_context, CUSTOMER_CONTEXT)
        self.pi_handle.reset_mock()

        self.agent.handle("What about Amazon?")

        self.pi_handle.assert_called_once_with(
            "What about Amazon?", CUSTOMER_CONTEXT, (), conversation_history=[]
        )

    def test_failed_pi_answer_does_not_change_completed_context(self):
        question = "How much did I spend last month?"
        self.agent.handle(question)
        self.pi_handle.side_effect = RuntimeError("Pi failed")
        with self.assertRaisesRegex(RuntimeError, "Pi failed"):
            self.agent.handle("Can I afford a $100 purchase?")
        self.assertEqual(self.agent.history, [{
            "user": question, "assistant": "Pi answer"
        }])

        self.pi_handle.side_effect = None
        self.agent.handle("What about Amazon?")

        self.assertEqual(self.pi_handle.call_args.args[2], ("get_transactions",))

    def test_history_keeps_ten_complete_turns(self):
        for index in range(12):
            self.agent.handle(f"Can I afford a ${index + 1} purchase?")

        self.assertEqual(self.agent.MAX_HISTORY_TURNS, 10)
        self.assertEqual(self.agent.history, [
            {
                "user": f"Can I afford a ${index + 1} purchase?",
                "assistant": "Pi answer",
            }
            for index in range(2, 12)
        ])
        self.assertEqual(len(self.pi_handle.call_args.kwargs["conversation_history"]), 10)

    def test_blank_messages_are_rejected_and_other_messages_are_stripped(self):
        for message in ("", "   ", "\n\t"):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.agent.handle(message)
        self.pi_handle.assert_not_called()
        self.common_handle.assert_not_called()
        self.assertEqual(self.agent.history, [])

        self.agent.handle("  What about Amazon?  ")

        self.pi_handle.assert_called_once_with(
            "What about Amazon?", CUSTOMER_CONTEXT, (), conversation_history=[]
        )
        self.assertEqual(self.agent.history[0]["user"], "What about Amazon?")


class InteractiveAppTests(unittest.TestCase):
    def test_continuous_questions_blank_lines_and_reset(self):
        agent = BankingAgent()
        with (
            patch("app.BankingAgent", return_value=agent),
            patch.object(agent.pi_agent, "handle", side_effect=[
                PiReply("First answer"), PiReply("Second answer")
            ]) as pi_handle,
            patch("app.getpass.getpass") as credentials,
            patch("builtins.input", side_effect=["", "first", "/reset", "second", "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            main()

        self.assertEqual(pi_handle.call_args_list, [
            call("first", None, (), conversation_history=[]),
            call("second", None, (), conversation_history=[]),
        ])
        credentials.assert_not_called()
        self.assertIn("First answer", output.getvalue())
        self.assertIn("Second answer", output.getvalue())
        self.assertIn("clear", output.getvalue().lower())

    def test_llm_auth_request_uses_hidden_credentials_and_replays_question(self):
        agent = BankingAgent()
        question = "What is my balance?"
        with (
            patch("app.BankingAgent", return_value=agent),
            patch.object(agent.pi_agent, "handle", return_value=PiReply("Please sign in", True)) as pi_handle,
            patch.object(agent.common_handler, "handle", return_value="Account answer") as common_handle,
            patch("app.getpass.getpass", side_effect=DEMO_CREDENTIALS[0]) as credentials,
            patch("builtins.input", side_effect=[question, question, "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            main()

        pi_handle.assert_called_once_with(
            question, None, (), conversation_history=[]
        )
        self.assertEqual(credentials.call_count, 2)
        self.assertEqual(common_handle.call_args_list, [
            call("account_balance", CUSTOMER_CONTEXT), call("account_balance", CUSTOMER_CONTEXT)
        ])
        self.assertEqual(agent.customer_context, CUSTOMER_CONTEXT)
        self.assertIn("Account answer", output.getvalue())
        for credential in DEMO_CREDENTIALS[0]:
            self.assertNotIn(credential, output.getvalue())
            self.assertNotIn(credential, str(agent.history))
            self.assertNotIn(credential, str(pi_handle.call_args_list))

    def test_sign_in_only_request_is_not_replayed_after_success(self):
        agent = BankingAgent()
        question = "Now I want to sign in"
        with (
            patch("app.BankingAgent", return_value=agent),
            patch.object(
                agent.pi_agent, "handle", return_value=PiReply("Please sign in", True)
            ) as pi_handle,
            patch("app.getpass.getpass", side_effect=DEMO_CREDENTIALS[0]),
            patch("builtins.input", side_effect=[question, "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            main()

        pi_handle.assert_called_once_with(question, None, (), conversation_history=[])
        self.assertEqual(agent.customer_context, CUSTOMER_CONTEXT)
        self.assertIn("Sign-in successful", output.getvalue())
        self.assertNotIn("already signed in", output.getvalue().lower())

    def test_three_invalid_credentials_exit_without_handling_next_question(self):
        agent = BankingAgent()
        with (
            patch("app.BankingAgent", return_value=agent),
            patch.object(agent.pi_agent, "handle", return_value=PiReply("Please sign in", True)) as pi_handle,
            patch.object(agent.common_handler, "handle") as common_handle,
            patch("app.getpass.getpass", side_effect=[DEMO_CREDENTIALS[0][0], "9999"] * 3) as credentials,
            patch("builtins.input", side_effect=["What is my balance?", "another question"]) as chat_input,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            main()

        self.assertEqual(credentials.call_count, 6)
        self.assertEqual(chat_input.call_count, 1)
        self.assertEqual(pi_handle.call_count, 1)
        self.assertEqual(agent.failed_auth_attempts, 3)
        self.assertTrue(agent.closed)
        common_handle.assert_not_called()

    def test_credentials_can_succeed_after_two_failures(self):
        agent = BankingAgent()
        with (
            patch("app.BankingAgent", return_value=agent),
            patch.object(agent.pi_agent, "handle", return_value=PiReply("Please sign in", True)) as pi_handle,
            patch.object(agent.common_handler, "handle", return_value="Account answer") as common_handle,
            patch("app.getpass.getpass", side_effect=[
                DEMO_CREDENTIALS[0][0], "9999", DEMO_CREDENTIALS[0][0], "9999",
                *DEMO_CREDENTIALS[0],
            ]) as credentials,
            patch("builtins.input", side_effect=["What is my balance?", "/exit"]),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            main()

        self.assertEqual(credentials.call_count, 6)
        self.assertEqual(pi_handle.call_count, 1)
        self.assertEqual(agent.failed_auth_attempts, 2)
        self.assertFalse(agent.closed)
        common_handle.assert_called_once_with("account_balance", CUSTOMER_CONTEXT)

    def test_errors_are_printed_and_next_question_is_answered(self):
        agent = BankingAgent()
        with (
            patch("app.BankingAgent", return_value=agent),
            patch.object(agent.pi_agent, "handle", side_effect=[
                RuntimeError("service unavailable"), ValueError("invalid question"),
                OSError("gateway unavailable"), PiReply("Fourth answer")
            ]) as pi_handle,
            patch("builtins.input", side_effect=["first", "second", "third", "fourth", "quit"]),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            main()

        self.assertEqual(pi_handle.call_count, 4)
        self.assertIn("Error: service unavailable", output.getvalue())
        self.assertIn("Error: invalid question", output.getvalue())
        self.assertIn("Error: gateway unavailable", output.getvalue())
        self.assertIn("Fourth answer", output.getvalue())

    def test_all_exit_commands_end_before_handling(self):
        for command in ("/exit", "/quit", "exit", "quit"):
            with (
                self.subTest(command=command),
                patch("app.BankingAgent") as agent_class,
                patch("builtins.input", return_value=command),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                main()
                agent_class.return_value.handle.assert_not_called()

    def test_eof_and_keyboard_interrupt_exit_cleanly(self):
        for exception in (EOFError, KeyboardInterrupt):
            with (
                self.subTest(exception=exception),
                patch("app.BankingAgent") as agent_class,
                patch("builtins.input", side_effect=exception),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                main()
                agent_class.return_value.handle.assert_not_called()

    def test_eof_and_keyboard_interrupt_during_credentials_exit_cleanly(self):
        for exception in (EOFError, KeyboardInterrupt):
            agent = BankingAgent()
            with (
                self.subTest(exception=exception),
                patch("app.BankingAgent", return_value=agent),
                patch.object(agent.pi_agent, "handle", return_value=PiReply("Sign in", True)),
                patch("builtins.input", return_value="What is my balance?") as chat_input,
                patch("app.getpass.getpass", side_effect=exception),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                main()
            self.assertEqual(chat_input.call_count, 1)
            self.assertIsNone(agent.customer_context)
            self.assertEqual(agent.failed_auth_attempts, 0)


if __name__ == "__main__":
    unittest.main()
