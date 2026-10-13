#!/usr/bin/env python3
"""Conservative Bank Agent Fast Path router for LM Studio + Qwen3.5-0.8B-MLX.

Install: pip install 'openai>=1.0' 'pydantic>=2.0'
Run: python bank_fast_path_router.py
     python bank_fast_path_router.py 'If I spend 160, how much remains?' --balance 3000
     python bank_fast_path_router.py --model '<LM Studio model id>'

This script NEVER connects to a bank or moves money. It only routes requests and
optionally demonstrates hypothetical calculations using --balance.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Literal
from text_to_num import alpha2digit


from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PlanId = Literal["get_balance", "balance_after_spending", "project_balance", "unknown"]

# Only these reviewed templates are eligible for Fast Path. This is NOT a tool planner.
PLANS = {
    "get_balance": "Query the CURRENT account balance. No other operation.",
    "balance_after_spending": "Calculate remaining balance after ONE hypothetical one-time expense. Requires amount.",
    "project_balance": "Calculate remaining balance after ONE hypothetical fixed MONTHLY expense over N months/years; no income, interest or other changes.",
}

class Slots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str | int | float | None = None                 # decimal string, e.g. "160.50"
    frequency: Literal["monthly"] | None = None
    duration_value: int | None = None         # e.g. 10
    duration_unit: Literal["month", "year"] | None = None

class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: PlanId
    slots: Slots
    fully_covered: bool                     # Does chosen plan express the ENTIRE request?
    reason: str = Field(default="", max_length=180)

SYSTEM_PROMPT = """You are a CONSERVATIVE bank request router, not a general assistant.
Select ONE existing plan ONLY if it covers the ENTIRE user request. Otherwise choose unknown.
Available reviewed plans:
- get_balance: Query the current account balance only. No account selection, dates, or other actions.
- balance_after_spending: Compute balance after exactly ONE hypothetical one-time expense. Required: amount.
- project_balance: Compute balance after exactly ONE hypothetical fixed MONTHLY expense for a specified duration in months or years. Required: amount, frequency=monthly, duration_value, duration_unit. No interest, income, deposits, other expenses, or compound conditions.

Output a single JSON object ONLY, with EXACTLY these keys:
{"plan_id":"get_balance|balance_after_spending|project_balance|unknown",
 "slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null},
 "fully_covered":true,"reason":"brief reason"}

Rules:
- Understand English. Do NOT answer the question or execute tools.
- amount is a decimal STRING, never a JSON number. Copy numerical value from user; do not invent it.
- duration_value is the original integer duration (10 years => 10, unit year), do not convert years to months.
- If request asks for transfers, real payments, multiple expenses, comparisons, filters, conditional logic, interest, deposits, transactions, unsupported cadence, or additional operations: unknown, fully_covered=false, slots all null.
- If any required field is absent or ambiguous: unknown.
- Do not choose a plan just because words are similar. Be conservative.
- Hypothetical spending questions such as "If I pay X, how much
  will remain?" belong to balance_after_spending.
- "If" alone does not make a request unsupported conditional logic.
- Distinguish calculating the effect of a payment from asking
  you to execute a payment. Only actual payment instructions
  are unsupported.

Examples:
User: What is my balance?
Output: {"plan_id":"get_balance","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null},"fully_covered":true,"reason":"current balance"}
User: If I spend 160 dollars, how much remains?
Output: {"plan_id":"balance_after_spending","slots":{"amount":"160","frequency":null,"duration_value":null,"duration_unit":null},"fully_covered":true,"reason":"one hypothetical expense"}
User: If I spend 20 dollars each month, how much remains after 10 years?
Output: {"plan_id":"project_balance","slots":{"amount":"20","frequency":"monthly","duration_value":10,"duration_unit":"year"},"fully_covered":true,"reason":"one monthly expense"}
User: If I spend 100 and then 160, how much remains?
Output: {"plan_id":"unknown","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null},"fully_covered":false,"reason":"multiple operations"}
User: If I pay 1000 dollars, how much will be remained in my account?
Output: {"plan_id":"balance_after_spending","slots":{"amount":"1000","frequency":null,"duration_value":null,"duration_unit":null},"fully_covered":true,"reason":"one hypothetical expense"}
"""

# Conservative exclusion patterns. False negatives are fine: Pi Agent handles them.
# These are safeguards, NOT a complete semantic correctness proof.
COMPLEX_RE = re.compile(
    r"\b(?:transfer|send money|pay someone|interest|loan|deposit|refund|compare|"
    r"weekly|daily|quarterly|annually|income|and then|after that|also)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![0-9A-Za-z_.])\d+(?:\.\d+)?(?![0-9A-Za-z_.])")
MONTHLY_RE = re.compile(r"monthly|per month|each month", re.I)
YEAR_RE = re.compile(r"years?", re.I)
MONTH_RE = re.compile(r"months?", re.I)
SPENDING_RE = re.compile(
    r"\b(?:spend|spent|expense|expenses|pay|paid|payment)\b",
    re.I
)


def unknown(reason: str) -> Route:
    return Route(plan_id="unknown", slots=Slots(), fully_covered=False, reason=reason[:180])


def extract_json(content: str) -> dict:
    # Some model templates include <think>...</think> even when asked not to.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    decoder = json.JSONDecoder()
    for i, char in enumerate(content):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(content[i:])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    raise ValueError("No JSON object in model response")

def normalize_written_amounts(message: str) -> str:
    """Normalize English number words before routing and validation."""
    if not isinstance(message, str):
        raise TypeError("message must be a string")

    # Normalize repeated spaces, tabs, and non-breaking spaces.
    text = " ".join(message.split())

    # threshold=0 also converts small numbers such as "one" and "two".
    return alpha2digit(text, "en", threshold=0)

def choose_model(client: OpenAI, preferred: str | None) -> str:
    if preferred:
        return preferred
    models = [m.id for m in client.models.list().data]
    if not models:
        raise RuntimeError("LM Studio returned no models. Load a model and start Local Server.")
    matching = [m for m in models if "qwen3.5" in m.lower() and "0.8b" in m.lower()]
    if matching:
        return matching[0]
    if len(models) == 1:
        return models[0]
    raise RuntimeError(f"Multiple models loaded. Specify --model. Available: {models}")


def parse_with_lmstudio(client: OpenAI, model_id: str, message: str) -> Route:
    response = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        temperature=0,
        max_tokens=260,
    )
    print(f"LM Studio response: {response.choices[0].message.content}")
    content = response.choices[0].message.content or ""
    return Route.model_validate(extract_json(content))


def validate_route(message: str, route: Route) -> tuple[bool, str]:
    if route.plan_id not in PLANS or not route.fully_covered:
        return False, "No fully covered reviewed plan"
    if COMPLEX_RE.search(message):
        return False, "Potential additional or unsupported operation"

    slots = route.slots
    nums = [Decimal(x) for x in NUMBER_RE.findall(message)]

    if route.plan_id == "get_balance":
        if nums or any(v is not None for v in slots.model_dump().values()):
            return False, "Balance-only plan must not contain numbers or slots"
        return True, ""

    try:
        if slots.amount is None:
            return False, "Missing amount"

        amount_str = str(slots.amount)

        if not re.fullmatch(r"\d+(?:\.\d{1,2})?", amount_str):
            return False, "Invalid amount"

        amount = Decimal(amount_str)
        if not amount.is_finite() or not (Decimal("0") < amount <= Decimal("1000000")):
            return False, "Amount outside reviewed limits"
    except (InvalidOperation, ValueError):
        return False, "Invalid decimal amount"

    if not SPENDING_RE.search(message):
        return False, "Expense wording not confirmed"

    if route.plan_id == "balance_after_spending":
        if slots.frequency is not None or slots.duration_value is not None or slots.duration_unit is not None:
            return False, "Unexpected extra slots"
        if len(nums) != 1 or nums[0] != amount:
            return False, "Amount does not match unique number in request"
        if MONTHLY_RE.search(message):
            return False, "Recurring expense requires different plan"
        return True, ""

    if route.plan_id == "project_balance":
        if slots.frequency != "monthly" or not MONTHLY_RE.search(message):
            return False, "Only explicitly monthly recurrence supported"
        n = slots.duration_value
        if n is None or not (1 <= n <= 1200):
            return False, "Invalid duration"
        if slots.duration_unit == "year":
            if not YEAR_RE.search(message) or n > 100:
                return False, "Year duration not confirmed"
        elif slots.duration_unit == "month":
            if not MONTH_RE.search(message):
                return False, "Month duration not confirmed"
        else:
            return False, "Duration unit required"
        if len(nums) != 2 or sorted(nums) != sorted([amount, Decimal(n)]):
            return False, "Amount/duration not matched to original numbers"
        return True, ""

    return False, "Unrecognized plan"


def route_request(client: OpenAI, model_id: str, message: str) -> tuple[Route, float]:
    start = time.perf_counter()
    try:
        normalized_message = normalize_written_amounts(message)
        print(f"Normalized input: {normalized_message}")
        proposal = parse_with_lmstudio(client, model_id, normalized_message)
        print(f"proposal: {proposal.model_dump()}")
        valid, reason = validate_route(normalized_message, proposal)
        result = proposal if valid else unknown(reason)
    except (ValueError, ValidationError, KeyError, IndexError) as exc:
        result = unknown(f"Model output invalid: {type(exc).__name__}: {exc}")
    return result, (time.perf_counter() - start) * 1000


def preview(route: Route, balance: Decimal | None) -> dict:
    if route.plan_id == "unknown":
        return {"next": "pi_agent", "message": "Fallback to Pi Agent; no bank action executed"}
    if balance is None:
        return {"next": "bank_tool", "plan": route.plan_id,
                "message": "Validated route only. Connect your read-only balance tool in your application."}
    if route.plan_id == "get_balance":
        remaining = balance
    elif route.plan_id == "balance_after_spending":
        remaining = balance - Decimal(route.slots.amount)
    else:
        months = route.slots.duration_value * (12 if route.slots.duration_unit == "year" else 1)
        remaining = balance - Decimal(route.slots.amount) * months
    return {"next": "hypothetical_preview", "starting_balance": str(balance),
            "remaining_balance": str(remaining), "note": "No bank transaction was performed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("message", nargs="?", help="One question; omit for interactive mode")
    parser.add_argument("--base-url", default=os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"))
    parser.add_argument("--model", default=os.getenv("LMSTUDIO_MODEL", "qwen3.5-0.8b-mlx"), help="Exact LM Studio model id")
    parser.add_argument("--balance", type=Decimal, help="Optional mock balance for hypothetical preview")
    args = parser.parse_args()
    client = OpenAI(base_url=args.base_url, api_key="lm-studio", timeout=45.0, max_retries=0)
    try:
        model_id = choose_model(client, args.model)
    except Exception as exc:
        print(f"LM Studio setup error: {exc}", file=sys.stderr)
        return 1
    print(f"LM Studio: {args.base_url} | model: {model_id}", file=sys.stderr)

    def run(message: str) -> None:
        try:
            route, elapsed_ms = route_request(client, model_id, message)
            print(json.dumps({"input": message, "route": route.model_dump(),
                              "elapsed_ms": round(elapsed_ms, 1),
                              "execution": preview(route, args.balance)}, ensure_ascii=False, indent=2))
        except Exception as exc:
            print(json.dumps({"input": message, "route": unknown("LM Studio unavailable").model_dump(),
                              "error": str(exc), "execution": {"next": "pi_agent"}},
                             ensure_ascii=False, indent=2))

    if args.message:
        run(args.message)
    else:
        print("Enter a banking question (quit to exit):", file=sys.stderr)
        while True:
            try:
                message = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message.lower() in {"quit", "exit", "q"}:
                break
            if message:
                run(message)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
