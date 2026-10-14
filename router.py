import json
import os
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
            proposed_tools = self.PLAN_TOOLS.get(proposal.plan_id) if proposal else None
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
            tools=proposed_tools or self.FALLBACK_TOOLS,
            retry_tools=self.FALLBACK_TOOLS if proposed_tools else (),
            reason=reason,
        )
