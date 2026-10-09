import re
from dataclasses import dataclass
from enum import Enum


class IntentType(str, Enum):
    COMMON = "common"
    COMPLEX = "complex"


@dataclass
class RouteResult:
    intent_type: IntentType
    capability: str
    confidence: float
    tools: tuple[str, ...] = ()


class CapabilityRouter:
    def route(self, message: str) -> RouteResult:
        text = message.lower()
        recent_match = re.search(
            r"\b(?:last|latest)\s+(\d+)\s+transactions?\b", text
        )

        complex_request = any(
            word in text for word in (
                "afford", "enough", "mortgage", "upcoming", "after",
                "spent", "spend", "spending", "saving", "saved", "search",
                "compare", "can i", "买吗",
            )
        )

        # Common intents should be handled without full agent reasoning.
        if not complex_request and (
            "credit card balance" in text
            or "minimum payment" in text
            or "信用卡余额" in text
        ):
            return RouteResult(
                intent_type=IntentType.COMMON,
                capability="credit_card_summary",
                confidence=0.95,
            )

        if not complex_request and (
            "balance" in text or "多少钱" in text or "余额" in text
        ):
            return RouteResult(
                intent_type=IntentType.COMMON,
                capability="account_balance",
                confidence=0.98,
            )

        if not complex_request and (
            "recent transaction" in text
            or "recent transactions" in text
            or "最近交易" in text
        ):
            return RouteResult(
                intent_type=IntentType.COMMON,
                capability="recent_transactions",
                confidence=0.95,
            )

        # shortcut: Keyword routing can miss new phrasing; use a classifier if coverage grows.
        tools = []
        if any(word in text for word in (
            "balance", "afford", "enough", "can i buy", "can i purchase",
            "mortgage", "付款"
        )):
            tools.append("get_balance")
        if any(word in text for word in ("mortgage", "upcoming", "payment coming")):
            tools.append("get_upcoming_payments")
        transaction_query = any(word in text for word in (
            "transaction", "spent", "spend", "spending", "saving", "saved",
            "merchant", "search"
        ))
        time_range = any(word in text for word in (
            "month", "week", "year", "between", "from ", "since", "date"
        ))
        transaction_query = transaction_query or "purchase" in text and (
            time_range or "list" in text or "show" in text
        )
        merchant_query = " at " in text or "merchant" in text or "search" in text
        total_query = any(word in text for word in (
            "how much", "total", "overall", "all", "saving", "saved"
        ))
        if recent_match:
            tools.append("get_transactions")
        elif transaction_query and (time_range or (total_query and not merchant_query)):
            tools.append("get_transactions")
        elif transaction_query:
            tools.append("search_transactions")
        if any(word in text for word in ("card", "minimum payment", "信用卡")):
            tools.append("get_credit_card_summary")

        return RouteResult(
            intent_type=IntentType.COMPLEX,
            capability="banking_reasoning",
            confidence=0.80,
            tools=tuple(tools),
        )
