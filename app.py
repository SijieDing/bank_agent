import getpass
import os
from uuid import uuid4
from pathlib import Path

from workflows.store import WorkflowStore
from workflows.manager import WorkflowManager
from workflows.parser import TransferParser
import threading

from authentication import contains_credentials
from router import CapabilityRouter, IntentType
from gateway import BankingToolGateway
from common_intents import CommonIntentHandler
from pi_agent import PiBankingAgent


class BankingAgent:
    MAX_HISTORY_TURNS = 10
    MAX_AUTH_ATTEMPTS = 3
    COMMON_TOOLS = {
        "account_balance": ("get_balance",),
        "recent_transactions": ("get_transactions",),
        "credit_card_summary": ("get_credit_card_summary",),
        "get_balance": ("get_balance",),
        "balance_after_spending": ("get_balance",),
        "project_balance": ("get_balance",),
        "query_transactions": ("get_transactions",),
        "latest_transaction": ("get_transactions",),
        "contact_information": ("get_contact_information",),
    }

    def __init__(self, workflow_db=None, conversation_id=None, bank_api_url=None):
        self.router = CapabilityRouter()
        self.gateway = BankingToolGateway(base_url=bank_api_url)
        self.conversation_id = conversation_id or os.getenv("BANK_CONVERSATION_ID") or uuid4().hex
        self.workflow_store = WorkflowStore(workflow_db or os.getenv(
            "BANK_WORKFLOW_DB", str(Path.home() / ".bank_agent" / "workflows.sqlite3")))
        self._workflow = None
        self.common_handler = CommonIntentHandler(self.gateway)
        self.pi_agent = PiBankingAgent(self.gateway)
        self.history: list[dict[str, str]] = []
        self._previous_tools: tuple[str, ...] = ()
        self._customer_context: dict[str, str] | None = None
        self._authentication_requested = False
        self._replay_after_authentication = False
        self._failed_auth_attempts = 0
        self._closed = False
        self._lock = threading.RLock()

    @property
    def workflow(self):
        if self._workflow is None:
            self._workflow = WorkflowManager(
                self.gateway, self.workflow_store,
                TransferParser(self.router.client, self.router.model_id), self.conversation_id)
        return self._workflow

    def close(self):
        self.gateway.close()
        if self._workflow:
            self._workflow.close()

    @property
    def customer_context(self) -> dict[str, str] | None:
        return self._customer_context.copy() if self._customer_context else None

    @property
    def authentication_requested(self) -> bool:
        return self._authentication_requested

    @property
    def failed_auth_attempts(self) -> int:
        return self._failed_auth_attempts

    @property
    def replay_after_authentication(self) -> bool:
        return self._replay_after_authentication

    @property
    def closed(self) -> bool:
        return self._closed

    def reset(self) -> str:
        # Clearing messages must not allow account switching or bypass the attempt limit.
        with self._lock:
            self.history.clear()
            self._previous_tools = ()
            if self._customer_context:
                return self.workflow.reset(self._customer_context["customer_id"])
            return ""

    def authenticate(self, card_number: str, pin: str) -> bool:
        with self._lock:
            if self._closed:
                raise ValueError("This conversation has ended")
            if self._customer_context is not None:
                raise ValueError("This conversation is already signed in; switching users is not allowed")
            if not self._authentication_requested:
                raise ValueError("The agent must request secure sign-in first")

            context = self.gateway.authenticate(card_number, pin)
            if context is None:
                self._failed_auth_attempts += 1
                if self._failed_auth_attempts >= self.MAX_AUTH_ATTEMPTS:
                    self._closed = True
                    self._authentication_requested = False
                    self._replay_after_authentication = False
                    self.reset()
                return False

            self._customer_context = context.copy()
            self._authentication_requested = False
            self._replay_after_authentication = False
            return True

    def handle(self, message: str) -> str:
        with self._lock:
            if self._closed:
                raise ValueError("This conversation has ended")
            message = message.strip()
            if not message:
                raise ValueError("Message cannot be empty")
            if contains_credentials(message):
                raise ValueError("Do not enter card numbers or PINs in chat. Use the secure sign-in prompts")
            if self._authentication_requested:
                return "Use the secure card and PIN prompts to finish signing in."

            task = self.workflow.get_task(self._customer_context["customer_id"]) if self._customer_context else None
            if task and not task.terminal:
                response = self.workflow.handle(message, self.customer_context)
                if response is not None:
                    self.history.append({"user": message, "assistant": response})
                    del self.history[:-self.MAX_HISTORY_TURNS]
                    return response

            route = self.router.route(message)
            print(
                f"Route: {route.intent_type.value}, "
                f"Capability: {route.capability}, "
                f"Confidence: {route.confidence:.2f}"
            )

            if route.capability == "transfer_money":
                if self._customer_context is None:
                    self._authentication_requested = True
                    self._replay_after_authentication = True
                    return "Please sign in using the hidden local prompts to start your transfer."
                answer = self.workflow.handle(message, self.customer_context, starting=True)
                if answer is None:
                    answer = "Please clarify your transfer request."
                tools = ()
            elif route.capability == "contact_information":
                answer = self.common_handler.handle(route.capability, None)
                tools = ()
            elif self._customer_context is None:
                tools = (
                    route.tools
                    if route.tools and set(route.tools) <= set(PiBankingAgent.PUBLIC_TOOLS)
                    else ()
                )
                print(f"Pi tools: {tools}")
                reply = self.pi_agent.handle(
                    message, None, tools,
                    conversation_history=self.history.copy(),
                )
                answer = reply.answer
                self._authentication_requested = reply.authentication_requested
                self._replay_after_authentication = reply.authentication_requested and (
                    route.intent_type == IntentType.COMMON or bool(route.tools)
                )
            elif route.intent_type == IntentType.COMMON:
                if route.capability in (
                    "account_balance", "recent_transactions", "credit_card_summary"
                ):
                    answer = self.common_handler.handle(route.capability, self.customer_context)
                else:
                    answer = self.common_handler.handle(
                        route.capability, self.customer_context, route.slots
                    )
                tools = self.COMMON_TOOLS[route.capability]
            else:
                tools = (
                    self._previous_tools
                    if route.tools == self.router.FALLBACK_TOOLS and self._previous_tools
                    else route.tools or self._previous_tools
                )
                try:
                    print(f"Pi tools: {tools}")
                    reply = self.pi_agent.handle(
                        message, self.customer_context, tools,
                        conversation_history=self.history.copy(),
                    )
                except RuntimeError:
                    if not route.retry_tools or route.retry_tools == tools:
                        raise
                    tools = route.retry_tools
                    print(f"Pi tools: {tools}")
                    reply = self.pi_agent.handle(
                        message, self.customer_context, tools,
                        conversation_history=self.history.copy(),
                    )
                answer = reply.answer

            self.history.append({"user": message, "assistant": answer})
            del self.history[:-self.MAX_HISTORY_TURNS]
            self._previous_tools = tools
            return answer


def main() -> None:
    agent = BankingAgent()

    print(f"Conversation ID: {agent.conversation_id}. Set BANK_CONVERSATION_ID to resume after restart.")
    print("Banking chat. Enter /reset to clear messages or /exit to quit.")
    print("Enter card numbers and PINs only in the hidden secure sign-in prompts.")
    while True:
        try:
            message = input("\nYou: ").strip()
            if not message:
                continue
            if message.lower() in ("/exit", "/quit", "exit", "quit"):
                print("Goodbye.")
                break
            if message.lower() == "/reset":
                notice = agent.reset()
                print("Conversation cleared." + (notice or ""))
                continue
            answer = agent.handle(message)
            print("Agent:", answer)
            while agent.authentication_requested:
                replay_question = agent.replay_after_authentication
                card_number = getpass.getpass("Card number (hidden): ")
                pin = getpass.getpass("PIN (hidden): ")
                signed_in = agent.authenticate(card_number, pin)
                del card_number, pin
                if signed_in:
                    print("Sign-in successful. This conversation is locked to this user.")
                    if replay_question:
                        print("Agent:", agent.handle(message))
                elif agent.closed:
                    print("Three failed sign-in attempts. Conversation ended.")
                else:
                    remaining = agent.MAX_AUTH_ATTEMPTS - agent.failed_auth_attempts
                    print(f"Invalid card number or PIN. {remaining} attempts remaining.")
            if agent.closed:
                break
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        except (RuntimeError, ValueError, OSError) as exc:
            print("Error:", exc)

    agent.close()


if __name__ == "__main__":
    main()
