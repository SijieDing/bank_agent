import json
import subprocess
import unittest
from datetime import date, timedelta
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gateway import BankingToolGateway
from pi_agent import PiBankingAgent, PiReply


class RecordingGateway(BankingToolGateway):
    def __init__(self):
        self.calls = []

    def get_balance(self, account_id):
        self.calls.append(("get_balance", account_id))
        return {"account_id": account_id, "available_balance": 123.45, "currency": "CAD"}

    def get_contact_information(self):
        self.calls.append(("get_contact_information",))
        return {
            "phone": "+1-800-555-0142",
            "address": "123 Bank Street, Toronto, ON M5V 1J1",
            "hours": "Monday to Friday, 9:00 AM to 5:00 PM (Eastern)",
        }


def pi_result(text="Pi answer", stop_reason="stop", error_message=None):
    message = {
        "role": "assistant",
        "content": ([{"type": "text", "text": text}] if text else []),
        "stopReason": stop_reason,
    }
    if error_message:
        message["errorMessage"] = error_message
    return subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"type": "message_end", "message": message}) + "\n", stderr="",
    )


class PiBankingAgentTests(unittest.TestCase):
    def test_pi_receives_completed_turns_as_context_without_a_saved_session(self):
        history = [{
            "user": "Can I afford a CAD 8000 laptop?",
            "assistant": "This account cannot cover the purchase in full.",
        }]
        message = "What about CAD 4000 instead?"

        def fake_pi(command, **kwargs):
            self.assertIn("--no-session", command)
            prompt = command[-1]
            prefix = "Previous conversation (JSON; context only):\n"
            self.assertTrue(prompt.startswith(prefix))
            previous, current = prompt[len(prefix):].split(
                "\n\nCurrent customer message:\n", 1
            )
            self.assertEqual(json.loads(previous), history)
            self.assertEqual(current, message)
            policy = command[command.index("--system-prompt") + 1]
            self.assertIn("Fetch current banking", policy)
            self.assertNotIn(history[0]["user"], policy)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIn("json", command)
            return pi_result()

        with (
            patch("pi_agent.ThreadingHTTPServer") as bridge,
            patch("pi_agent.subprocess.run", side_effect=fake_pi),
        ):
            bridge.return_value.__enter__.return_value.server_port = 12345
            answer = PiBankingAgent(BankingToolGateway()).handle(
                message,
                {"default_account_id": "CHK-001", "default_card_id": "VISA-001"},
                ("get_balance",),
                conversation_history=history,
            )
        self.assertEqual(answer, PiReply("Pi answer"))

    def test_pi_uses_current_customers_gateway_and_rejects_unauthorized_calls(self):
        gateway = RecordingGateway()

        def fake_pi(command, *, env, **kwargs):
            self.assertIn("--no-extensions", command)
            self.assertEqual(command[command.index("--tools") + 1], "get_balance")
            policy = command[command.index("--system-prompt") + 1]
            self.assertIn("## Response pattern", policy)
            self.assertIn("Do not recommend products", policy)
            url = env["BANK_AGENT_URL"]

            def call(token, tool="get_balance", **args):
                request = Request(
                    url,
                    data=json.dumps({"tool": tool, **args}).encode(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                return urlopen(request, timeout=2)

            with self.assertRaises(HTTPError) as error:
                call("wrong-token")
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                call(env["BANK_AGENT_TOKEN"], "get_upcoming_payments")
            self.assertEqual(error.exception.code, 403)
            with call(env["BANK_AGENT_TOKEN"], account_id="CHK-002", card_id="VISA-002") as response:
                self.assertEqual(json.load(response), {
                    "available_balance": 123.45, "currency": "CAD"
                })
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            return pi_result()

        with patch("pi_agent.subprocess.run", side_effect=fake_pi):
            answer = PiBankingAgent(gateway).handle(
                "Can I afford it?",
                {"default_account_id": "CHK-001", "default_card_id": "VISA-001"},
                ("get_balance",),
            )

        self.assertEqual(answer, PiReply("Pi answer"))
        self.assertEqual(gateway.calls, [("get_balance", "CHK-001")])

    def test_unauthenticated_model_decides_sign_in_without_tools(self):
        agent = PiBankingAgent(BankingToolGateway())
        with patch.object(agent, "_run_pi", return_value="AUTHENTICATE") as run:
            reply = agent.handle("What is my balance?", None, ())

        self.assertEqual(reply, PiReply(
            "Please enter your card number and PIN in the hidden secure sign-in prompts.", True,
        ))
        self.assertEqual(run.call_args.args[2], ())
        self.assertIn("Return exactly one word", run.call_args.args[1])

    def test_public_request_is_answered_without_banking_tools(self):
        agent = PiBankingAgent(BankingToolGateway())
        with patch.object(agent, "_run_pi", side_effect=["PUBLIC", "General answer"]) as run:
            reply = agent.handle("Hello", None, ())

        self.assertEqual(reply, PiReply("General answer"))
        self.assertEqual([call.args[2] for call in run.call_args_list], [(), ()])

    def test_public_contact_tool_uses_gateway_without_authentication(self):
        gateway = RecordingGateway()

        def fake_pi(command, *, env, **kwargs):
            self.assertEqual(
                command[command.index("--tools") + 1], "get_contact_information"
            )
            policy = command[command.index("--system-prompt") + 1]
            self.assertIn("does not require sign-in", policy)
            today = date.today()
            tomorrow = today + timedelta(days=1)
            self.assertIn(
                f"Today's date is {today.isoformat()} ({today.strftime('%A')}).",
                policy,
            )
            self.assertIn(
                f"Tomorrow is {tomorrow.isoformat()} ({tomorrow.strftime('%A')}).",
                policy,
            )
            request = Request(
                env["BANK_AGENT_URL"],
                data=json.dumps({"tool": "get_contact_information"}).encode(),
                headers={"Authorization": f"Bearer {env['BANK_AGENT_TOKEN']}"},
            )
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response)["address"],
                                 "123 Bank Street, Toronto, ON M5V 1J1")
            return pi_result("The office is at 123 Bank Street.")

        with patch("pi_agent.subprocess.run", side_effect=fake_pi) as run:
            reply = PiBankingAgent(gateway).handle(
                "Where is your location?", None, ("get_contact_information",)
            )

        self.assertEqual(reply, PiReply("The office is at 123 Bank Street."))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(gateway.calls, [("get_contact_information",)])

    def test_invalid_authentication_decision_is_retried_then_rejected(self):
        agent = PiBankingAgent(BankingToolGateway())
        with patch.object(agent, "_run_pi", side_effect=["Please sign in", "Please sign in"]) as run:
            with self.assertRaisesRegex(RuntimeError, "could not decide"):
                agent.handle("What is my balance?", None, ())

        self.assertEqual(run.call_count, 2)
        self.assertIn("No explanation", run.call_args.args[1])

    def test_unauthenticated_banking_and_unknown_auth_tool_are_rejected(self):
        agent = PiBankingAgent(BankingToolGateway())
        with patch("pi_agent.subprocess.run") as run:
            for tools in (("get_balance",), ("request_authentication",)):
                with self.subTest(tools=tools), self.assertRaises(ValueError):
                    agent.handle("What is my balance?", None, tools)
            run.assert_not_called()

    def test_location_route_selects_contact_fast_path(self):
        from router import CapabilityRouter, IntentType

        route = CapabilityRouter().route("Where is your location?")

        self.assertEqual(route.intent_type, IntentType.COMMON)
        self.assertEqual(route.capability, "contact_information")
        self.assertEqual(route.tools, ("get_contact_information",))

    def test_credentials_in_messages_or_user_history_are_rejected_before_pi(self):
        agent = PiBankingAgent(BankingToolGateway())
        cases = [
            ("My card is 4111 1111 1111 1111", []),
            ("1234", []),
            ("My PIN is 1234", []),
            ("Hello", [{"user": "My card is 4111111111111111", "assistant": "OK"}]),
        ]
        with patch("pi_agent.subprocess.run") as run:
            for message, history in cases:
                with self.subTest(message=message, history=history), self.assertRaises(ValueError):
                    agent.handle(message, None, (), history)
            run.assert_not_called()

    def test_generated_transaction_history_is_not_mistaken_for_credentials(self):
        history = [{
            "user": "Show all transactions in the past year",
            "assistant": (
                "10 transactions on account CHK-001 from October 9, 2025 through "
                "October 9, 2026. Coffee — Jun 30, 2026 — CAD 6.25."
            ),
        }]
        agent = PiBankingAgent(BankingToolGateway())
        with patch.object(agent, "_run_pi", return_value="Coffee was purchased"):
            reply = agent.handle(
                "Did I buy coffee before?",
                {"default_account_id": "CHK-001", "default_card_id": "VISA-001"},
                (), history,
            )
        self.assertEqual(reply, PiReply("Coffee was purchased"))

    def test_json_output_uses_latest_completed_assistant_and_isolates_stdin(self):
        earlier = json.loads(pi_result("Earlier answer").stdout)
        latest = json.loads(pi_result("Final answer").stdout)
        output = "\n".join([
            json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}}),
            json.dumps(earlier), json.dumps(latest),
        ])
        with patch("pi_agent.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, stdout=output, stderr="",
        )) as run:
            answer = PiBankingAgent(BankingToolGateway())._run_pi("Hello", "Policy", ())

        self.assertEqual(answer, "Final answer")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIn("--mode", run.call_args.args[0])
        self.assertIn("--no-tools", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("--thinking") + 1], "medium")

    def test_unparsed_tool_call_markup_is_not_returned_to_the_customer(self):
        markup = "<ifm|tool_calls>\n<ifm|tool_call>contact_info\n</ifm|tool_call>"
        with patch("pi_agent.subprocess.run", return_value=pi_result(markup)):
            with self.assertRaisesRegex(RuntimeError, "unparsed tool-call markup"):
                PiBankingAgent(BankingToolGateway())._run_pi("Hello", "Policy", ())

    def test_json_output_prints_completed_thinking_content(self):
        message = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Check the account data."},
                {"type": "text", "text": "Final answer"},
            ],
            "stopReason": "stop",
        }
        result = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"type": "message_end", "message": message}), stderr="",
        )

        with (
            patch("pi_agent.subprocess.run", return_value=result),
            patch("builtins.print") as output,
        ):
            answer = PiBankingAgent(BankingToolGateway())._run_pi("Hello", "Policy", ())

        self.assertEqual(answer, "Final answer")
        output.assert_called_once_with("Pi thinking:", "Check the account data.")

    def test_json_output_surfaces_errors_and_incomplete_answers(self):
        agent = PiBankingAgent(BankingToolGateway())
        for result, error in [
            (pi_result("", "error", "provider unavailable"), "provider unavailable"),
            (pi_result("partial", "length"), "before completing"),
        ]:
            with self.subTest(error=error), patch("pi_agent.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, error):
                    agent._run_pi("Hello", "Policy", ())

    def test_complex_routes_select_only_relevant_tools(self):
        from router import CapabilityRouter, IntentType

        router = CapabilityRouter()
        cases = {
            "Can I afford a $2000 laptop?": ("get_balance",),
            "Do I have enough for my mortgage payment?": (
                "get_balance", "get_upcoming_payments"
            ),
            "How much did I spend at Rogers?": ("search_transactions",),
            "How much I spend in last 3 months totally": ("get_transactions",),
            "Show all transactions from July to October": ("get_transactions",),
            "List all purchases last month": ("get_transactions",),
            "Compare my card balance with my account balance": (
                "get_balance", "get_credit_card_summary"
            ),
        }
        for message, tools in cases.items():
            with self.subTest(message=message):
                route = router.route(message)
                self.assertEqual(route.intent_type, IntentType.COMPLEX)
                self.assertEqual(route.tools, tools)

    def test_last_three_transactions_uses_get_transactions(self):
        from app import BankingAgent
        from router import IntentType

        agent = BankingAgent()
        message = "Tell me last 3 transactions"
        context = {
            "customer_id": "C10001", "default_account_id": "CHK-001", "default_card_id": "VISA-001",
        }
        route = agent.router.route(message)
        self.assertEqual(route.intent_type, IntentType.COMPLEX)
        self.assertEqual(route.tools, ("get_transactions",))
        with patch.object(agent.pi_agent, "handle", return_value=PiReply("Sign in", True)):
            agent.handle(message)
        self.assertTrue(agent.authenticate("4111111111111111", "1234"))
        agent.reset()
        with patch.object(agent.pi_agent, "handle", return_value=PiReply("Pi answer")) as pi_handle:
            answer = agent.handle(message)
        self.assertEqual(answer, "Pi answer")
        pi_handle.assert_called_once_with(
            message, context, ("get_transactions",), conversation_history=[]
        )

    def test_transactions_include_both_types_and_filtered_totals(self):
        gateway = BankingToolGateway()
        all_history = gateway.get_transactions("CHK-001")
        last_three = gateway.get_transactions("CHK-001", limit=3)
        both = gateway.get_transactions("CHK-001", "2026-07-08", "2026-10-08")
        savings = gateway.get_transactions(
            "CHK-001", "2026-07-08", "2026-10-08", "saving"
        )
        purchases = gateway.get_transactions(
            "CHK-001", "2026-07-08", "2026-10-08", "purchase"
        )

        self.assertEqual(len(all_history["transactions"]), 10)
        self.assertEqual(all_history["transaction_type"], "both")
        self.assertEqual(
            [tx["description"] for tx in last_three["transactions"]],
            ["Costco", "Rogers", "Amazon"],
        )
        self.assertEqual(last_three["total_saving"], "0.00")
        self.assertEqual(last_three["total_purchase"], "260.41")
        self.assertEqual(len(both["transactions"]), 9)
        self.assertEqual(both["transaction_type"], "both")
        self.assertEqual(both["total_saving"], "650.00")
        self.assertEqual(both["total_purchase"], "421.66")
        self.assertEqual(len(savings["transactions"]), 3)
        self.assertTrue(all(tx["type"] == "saving" for tx in savings["transactions"]))
        self.assertEqual((savings["total_saving"], savings["total_purchase"]),
                         ("650.00", "0.00"))
        self.assertEqual(len(purchases["transactions"]), 6)
        self.assertTrue(all(tx["type"] == "purchase" for tx in purchases["transactions"]))
        self.assertEqual((purchases["total_saving"], purchases["total_purchase"]),
                         ("0.00", "421.66"))
        with self.assertRaises(ValueError):
            gateway.get_transactions("CHK-001", "2026-10-08", "2026-07-08")
        with self.assertRaises(ValueError):
            gateway.get_transactions("CHK-001", transaction_type="transfer")
        with self.assertRaises(ValueError):
            gateway.get_transactions("CHK-001", "2026-02-30")
        with self.assertRaises(ValueError):
            gateway.get_transactions("CHK-001", limit=0)


if __name__ == "__main__":
    unittest.main()
