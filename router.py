import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from zoneinfo import ZoneInfo

from openai import OpenAI, OpenAIError

from bank_fast_path_router import Slots, preview, route_request_with_proposal


class IntentType(str, Enum):
    COMMON = "common"
    COMPLEX = "complex"
    WORKFLOW = "workflow"


@dataclass
class RouteResult:
    intent_type: IntentType
    capability: str
    confidence: float
    tools: tuple[str, ...] = ()
    slots: Slots = field(default_factory=Slots)
    reason: str = ""
    retry_tools: tuple[str, ...] = ()


class CapabilityRouter:
    """Route reviewed requests through the local small-model fast path."""

    PLAN_TOOLS = {
        "get_balance": ("get_balance",),
        "balance_after_spending": ("get_balance",),
        "project_balance": ("get_balance",),
        "query_transactions": ("get_transactions",),
        "latest_transaction": ("get_transactions",),
        "contact_information": ("get_contact_information",),
    }
    FALLBACK_TOOLS = (
        "get_balance",
        "get_transactions",
        "search_transactions",
        "get_credit_card_summary",
        "get_upcoming_payments",
        "get_contact_information",
    )

    def __init__(
        self,
        client: OpenAI | None = None,
        model_id: str | None = None,
        today: date | None = None,
        timezone_name: str | None = None,
    ):
        self.client = client or OpenAI(
            base_url=os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
            api_key="lm-studio",
            timeout=45.0,
            max_retries=0,
        )
        self.model_id = model_id or os.getenv("LMSTUDIO_MODEL", "qwen3.5-0.8b-mlx")
        self.today = today
        timezone_name = timezone_name or os.getenv("BANK_TIMEZONE")
        self.business_timezone = ZoneInfo(timezone_name) if timezone_name else None

    def route(self, message: str) -> RouteResult:
        simple = self._simple_route(message)
        if simple is not None:
            return simple

        try:
            today = self.today or datetime.now(self.business_timezone).date()
            route, elapsed_ms, proposal = route_request_with_proposal(
                self.client, self.model_id, message, today
            )
        except (OpenAIError, OSError, RuntimeError) as exc:
            return self._fallback(f"Fast-path model unavailable: {exc}")

        print(json.dumps({
            "input": message,
            "route": route.model_dump(),
            "elapsed_ms": round(elapsed_ms, 1),
            "execution": preview(route, None),
        }, ensure_ascii=False, indent=2))

        if proposal is not None and proposal.plan_id == "transfer_money":
            return RouteResult(IntentType.WORKFLOW, "transfer_money", 1.0)

        if route.plan_id == "unknown":
            inferred_tools = self._tools_for_message(message)
            proposed_tools = inferred_tools or (
                () if proposal is not None and proposal.plan_id != "unknown" else None
            )
            return self._fallback(route.reason, proposed_tools)

        return RouteResult(
            intent_type=IntentType.COMMON,
            capability=route.plan_id,
            confidence=1.0,
            tools=self.PLAN_TOOLS[route.plan_id],
            slots=route.slots,
            reason=route.reason,
        )

    def _fallback(
        self, reason: str, proposed_tools: tuple[str, ...] | None = None,
    ) -> RouteResult:
        return RouteResult(
            intent_type=IntentType.COMPLEX,
            capability="banking_reasoning",
            confidence=0.0,
            tools=self.FALLBACK_TOOLS if proposed_tools is None else proposed_tools,
            retry_tools=self.FALLBACK_TOOLS if proposed_tools is not None else (),
            reason=reason,
        )

    @staticmethod
    def _simple_route(message: str) -> RouteResult | None:
        text = message.lower()
        complex_request = any(word in text for word in (
            "afford", "enough", "mortgage", "upcoming", "after",
            "spent", "spend", "spending", "saving", "saved", "search",
            "compare", "can i", "买吗",
        ))
        if complex_request:
            return None
        if any(word in text for word in (
            "credit card balance", "minimum payment", "信用卡余额",
        )):
            return RouteResult(
                IntentType.COMMON, "credit_card_summary", 0.95,
                ("get_credit_card_summary",),
            )
        if "balance" in text or "多少钱" in text or "余额" in text:
            return RouteResult(
                IntentType.COMMON, "account_balance", 0.98, ("get_balance",),
            )
        if "recent transaction" in text or "最近交易" in text:
            return RouteResult(
                IntentType.COMMON, "recent_transactions", 0.95,
                ("get_transactions",),
            )
        return None

    @staticmethod
    def _tools_for_message(message: str) -> tuple[str, ...]:
        """Narrow fallback tools only when the request itself supports them."""
        text = message.lower()
        tools = []
        hypothetical_spending = (
            "remain" in text
            or "if " in text and any(word in text for word in ("spend", "pay", "buy"))
        )
        if any(word in text for word in (
            "balance", "afford", "enough", "can i buy", "can i purchase",
            "mortgage", "付款",
        )) or hypothetical_spending:
            tools.append("get_balance")
        if any(word in text for word in ("mortgage", "upcoming", "payment coming")):
            tools.append("get_upcoming_payments")

        transaction_query = not hypothetical_spending and any(word in text for word in (
            "transaction", "spent", "spend", "spending", "saving", "saved",
            "merchant", "search",
        ))
        time_range = any(word in text for word in (
            "month", "week", "year", "between", "from ", "since", "date",
        ))
        transaction_query = transaction_query or "purchase" in text and (
            time_range or "list" in text or "show" in text
        )
        merchant_query = " at " in text or "merchant" in text or "search" in text
        total_query = any(word in text for word in (
            "how much", "total", "overall", "all", "saving", "saved",
        ))
        if re.search(r"\b(?:last|latest)\s+\d+\s+transactions?\b", text):
            tools.append("get_transactions")
        elif transaction_query and (time_range or (total_query and not merchant_query)):
            tools.append("get_transactions")
        elif transaction_query:
            tools.append("search_transactions")
        if "card" in text or "minimum payment" in text or "信用卡" in text:
            tools.append("get_credit_card_summary")
        if re.search(
            r"\b(?:contact|phone|telephone|address|location|office|hours?|open|close)\b",
            text,
        ):
            tools.append("get_contact_information")
        return tuple(dict.fromkeys(tools))
