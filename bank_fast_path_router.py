#!/usr/bin/env python3
"""Conservative Bank Agent Fast Path router for LM Studio + Qwen3.5-0.8B-MLX.

Install: pip install 'openai>=1.0' 'pydantic>=2.0' 'text2num>=3.0'
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
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from decimal import Decimal, InvalidOperation
from typing import Literal
from text_to_num import alpha2digit


from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PlanId = Literal[
    "get_balance", "balance_after_spending", "project_balance",
    "query_transactions", "contact_information", "unknown",
]

# Only these reviewed templates are eligible for Fast Path. This is NOT a tool planner.
PLANS = {
    "get_balance": "Query the CURRENT account balance. No other operation.",
    "balance_after_spending": "Calculate remaining balance after ONE hypothetical one-time expense. Requires amount.",
    "project_balance": "Calculate remaining balance after ONE hypothetical fixed MONTHLY expense over N months/years; no income, interest or other changes.",
    "query_transactions": "Query transactions by date range, amount range, amount threshold, and/or store name.",
    "contact_information": "Return the bank's full phone number, office address, and opening hours.",
}

class Slots(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str | int | float | None = None                 # decimal string, e.g. "160.50"
    frequency: Literal["monthly"] | None = None
    duration_value: int | None = None         # e.g. 10
    duration_unit: Literal["month", "year"] | None = None
    start_date: str | None = None             # Inclusive YYYY-MM-DD, None = unbounded
    end_date: str | None = None               # Inclusive YYYY-MM-DD, None = unbounded
    minimum_amount: str | int | float | None = None
    maximum_amount: str | int | float | None = None
    store_name: str | None = None

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
- query_transactions: Query transactions using one or more supported filters: an inclusive ISO start/end date range, with either or both bounds; an inclusive minimum/maximum amount range; a strict amount-over threshold (minimum_amount only); or a store name.
- contact_information: Return the bank's complete contact information (phone number, office address, and opening hours). Use this for requests about any one or more of those contact details.

Output a single JSON object ONLY, with EXACTLY these keys:
{"plan_id":"get_balance|balance_after_spending|project_balance|query_transactions|contact_information|unknown",
 "slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},
 "fully_covered":true,"reason":"brief reason"}

Rules:
- Understand English. Do NOT answer the question or execute tools.
- amount is a decimal STRING, never a JSON number. Copy numerical value from user; do not invent it.
- minimum_amount and maximum_amount are decimal STRINGS, never JSON numbers.
- duration_value is the original integer duration (10 years => 10, unit year), do not convert years to months.
- For "between 100 and/to 200 dollars", set minimum_amount="100" and maximum_amount="200". For "over 100 dollars", set minimum_amount="100" and maximum_amount=null.
- Date filters are resolved by Python before this call. Copy the trusted date bounds supplied below exactly. Never calculate dates or invent a missing bound.
- If request asks for transfers, real payments, multiple expenses, unrelated comparisons, unsupported transaction filters, conditional logic, interest, deposits, or additional operations: unknown, fully_covered=false, slots all null.
- If any required field is absent or ambiguous: unknown.
- Do not choose a plan just because words are similar. Be conservative.
- Hypothetical spending questions such as "If I pay X, how much
  will remain?" belong to balance_after_spending.
- "If" alone does not make a request unsupported conditional logic.
- Distinguish calculating the effect of a payment from asking
  you to execute a payment. Only actual payment instructions
  are unsupported.
- A null date bound means unbounded, not missing required information.
- The marker [resolved date filter] represents a supported transaction date filter; it is not a store name.
- A transaction request with [resolved date filter] needs no amount or store to be fully covered.
- contact_information always returns all contact details even if the user asks for only the phone, location, office, or hours. It uses no slots and requires no sign-in.

Examples:
User: What is my balance?
Output: {"plan_id":"get_balance","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"current balance"}
User: If I spend 160 dollars, how much remains?
Output: {"plan_id":"balance_after_spending","slots":{"amount":"160","frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"one hypothetical expense"}
User: If I spend 20 dollars each month, how much remains after 10 years?
Output: {"plan_id":"project_balance","slots":{"amount":"20","frequency":"monthly","duration_value":10,"duration_unit":"year","start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"one monthly expense"}
User: If I spend 100 and then 160, how much remains?
Output: {"plan_id":"unknown","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":false,"reason":"multiple operations"}
User: If I pay 1000 dollars, how much will be remained in my account?
Output: {"plan_id":"balance_after_spending","slots":{"amount":"1000","frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"one hypothetical expense"}
User: Show my transactions from 2026-01-01 to 2026-10-10.
Output: {"plan_id":"query_transactions","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":"2026-01-01","end_date":"2026-10-10","minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"transaction date range"}
User: Show my transactions between 100 to 200 dollars.
Output: {"plan_id":"query_transactions","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":"100","maximum_amount":"200","store_name":null},"fully_covered":true,"reason":"transaction amount range"}
User: Show my transactions over 100 dollars.
Output: {"plan_id":"query_transactions","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":"100","maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"transaction amount threshold"}
User: Show my transactions about Amazon.
Output: {"plan_id":"query_transactions","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":"Amazon"},"fully_covered":true,"reason":"store transactions"}
User: Show my transactions about Amazon from 2026-01-01 to 2026-10-10.
Output: {"plan_id":"query_transactions","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":"2026-01-01","end_date":"2026-10-10","minimum_amount":null,"maximum_amount":null,"store_name":"Amazon"},"fully_covered":true,"reason":"store transactions in date range"}
User: What is your phone number?
Output: {"plan_id":"contact_information","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"bank contact information"}
User: When do you open?
Output: {"plan_id":"contact_information","slots":{"amount":null,"frequency":null,"duration_value":null,"duration_unit":null,"start_date":null,"end_date":null,"minimum_amount":null,"maximum_amount":null,"store_name":null},"fully_covered":true,"reason":"bank opening hours"}
"""

# Conservative exclusion patterns. False negatives are fine: Pi Agent handles them.
# These are safeguards, NOT a complete semantic correctness proof.
COMPLEX_RE = re.compile(
    r"\b(?:transfer|send money|pay someone|interest|loan|deposit|refund|compare|"
    r"weekly|daily|quarterly|annually|income|and then|after that|also)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![0-9A-Za-z_.])\d+(?:\.\d+)?(?![0-9A-Za-z_.])")
ISO_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
MONTHLY_RE = re.compile(r"monthly|per month|each month", re.I)
YEAR_RE = re.compile(r"years?", re.I)
MONTH_RE = re.compile(r"months?", re.I)
SPENDING_RE = re.compile(
    r"\b(?:spend|spent|expense|expenses|pay|paid|payment)\b",
    re.I
)
TRANSACTION_RE = re.compile(r"\btransactions?\b", re.I)
OVER_RE = re.compile(r"\b(?:over|above|more than|greater than)\b", re.I)
BETWEEN_RE = re.compile(r"\bbetween\b", re.I)
CONTACT_RE = re.compile(
    r"\b(?:contact (?:information|info|details)|phone(?: number)?|telephone(?: number)?|"
    r"address|location|office|(?:work|business|opening|office) hours?)\b|"
    r"\b(?:when|what time)\s+(?:do|does)\s+(?:you|the (?:bank|office))\s+open\b",
    re.I,
)



@dataclass(frozen=True)
class TransactionDates:
    """Resolved inclusive calendar dates plus input with the date phrase removed."""
    start_date: str | None
    end_date: str | None
    model_message: str
    non_date_message: str


ISO = r"\d{4}-\d{2}-\d{2}"
MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
NAMED_DATE = rf"\d{{4}}\s+(?:{'|'.join(MONTH_NAMES)})\s+\d{{1,2}}(?:st|nd|rd|th)?"
EXPLICIT_DATE = rf"(?:{ISO}|{NAMED_DATE})"
RECENT_DAYS = 10  # Product default: calendar dates including today.
# Ordered longest/specific phrases first. Unsupported time expressions fail closed.
TIME_PATTERNS = [
    ("range", rf"\b(?:from\s+{EXPLICIT_DATE}\s+(?:to|through|until)\s+{EXPLICIT_DATE}|between\s+{EXPLICIT_DATE}\s+and\s+{EXPLICIT_DATE})\b"),
    ("bound", rf"\b(?:on\s+or\s+before|on\s+or\s+after|before|after|since|until|through|on)\s+{ISO}\b"),
    ("rolling", r"\b(?:(?:in|for|over|during)\s+)?(?:the\s+)?(?:past|last|previous|recent)\s+\d+\s+(?:days?|weeks?|months?|years?)\b"),
    ("recent", r"\b(?:(?:(?:in|for|over|during)\s+)?(?:the\s+)?recent\s+days\b|recent(?=\s+transactions?\b))"),
    ("period", r"\b(?:(?:in|for|during)\s+)?(?:the\s+)?(?:this|last|previous)\s+(?:week|month|year)\b"),
    ("day", r"\b(?:today|yesterday|ytd|year-to-date)\b"),
]
# Intentionally conservative; a named month or an unhandled date must not disappear.
TIME_CUE_RE = re.compile(
    r"\b(?:past|last|previous|recent|today|yesterday|tomorrow|this|ago|now|ytd|year-to-date|"
    r"before|after|since|until|through|between|from|"
    r"days?|weeks?|months?|years?|hours?|minutes?|quarters?|weekends?|"
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?\b|"
    r"\b(?:in|during|for|on)\s+\d{4}\b|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b",
    re.I,
)


def shift_months(value: date, delta: int) -> date:
    """Calendar arithmetic: clamp Jan 31 minus one month / leap-day anniversaries."""
    index = value.year * 12 + value.month - 1 + delta
    year, month0 = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise ValueError("Date outside supported calendar")
    month = month0 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def parse_explicit_date(value: str) -> date:
    if re.fullmatch(ISO, value):
        return date.fromisoformat(value)
    match = re.fullmatch(
        rf"(\d{{4}})\s+({'|'.join(MONTH_NAMES)})\s+(\d{{1,2}})(?:st|nd|rd|th)?",
        value,
        re.I,
    )
    if not match:
        raise ValueError("Unsupported explicit date")
    year, month_name, day = match.groups()
    return date(int(year), MONTH_NAMES.index(month_name.lower()) + 1, int(day))


def resolve_transaction_dates(message: str, today: date) -> TransactionDates:
    """Supported grammar, independent of LLM output.

    past/last/recent N days/weeks: N/7N calendar dates including today.
    recent days / recent transactions: RECENT_DAYS (10) calendar dates including today.
    past N months/years: calendar subtraction through today, inclusive.
    last month/year/week: previous COMPLETE calendar period (weeks start Monday).
    this month/year/week: period start through today.
    before/after: exclusive input converted to inclusive date bounds.
    since/on or after and until/through/on or before: inclusive input.

    Dates refer to the configured business timezone. A database adapter must map
    end_date to < local midnight on the following day, not <= midnight of end_date.
    Multiple separate time expressions are deliberately sent to the fallback.
    """
    matches = []
    for kind, pattern in TIME_PATTERNS:
        for match in re.finditer(pattern, message, re.I):
            if not any(match.start() < old.end() and old.start() < match.end()
                       for _, old in matches):
                matches.append((kind, match))
    if len(matches) > 1:
        raise ValueError("Multiple date expressions require fallback")
    remainder = message
    if matches:
        kind, match = matches[0]
        remainder = message[:match.start()] + " " + message[match.end():]
    # 'between ... dollars' is an amount phrase, not an unsupported date.
    cue_text = re.sub(
        r"\bbetween\s+\$?\d+(?:\.\d+)?\s+(?:and|to)\s+\$?\d+(?:\.\d+)?(?:\s+dollars?)?\b",
        " ", remainder, flags=re.I,
    )
    if TIME_CUE_RE.search(cue_text):
        raise ValueError("Unsupported or ambiguous date wording; use an ISO date or supported relative period")
    if not matches:
        return TransactionDates(None, None, message, message)
    if re.search(r"\b(?:or|except|excluding|not)\b", remainder, re.I):
        raise ValueError("Date alternatives or exclusions require fallback")
    phrase = match.group().lower()
    start = end = None
    try:
        if kind == "range":
            start, end = [parse_explicit_date(x.group()) for x in re.finditer(EXPLICIT_DATE, phrase, re.I)]
        elif kind == "bound":
            boundary = date.fromisoformat(ISO_DATE_RE.search(phrase).group())
            operator = phrase[:phrase.index(boundary.isoformat())].strip()
            if operator == "before":
                end = boundary - timedelta(days=1)
            elif operator == "after":
                start = boundary + timedelta(days=1)
            elif operator in {"since", "on or after"}:
                start = boundary
            elif operator in {"until", "through", "on or before"}:
                end = boundary
            else:  # on
                start = end = boundary
        elif kind == "recent":
            start = today - timedelta(days=RECENT_DAYS - 1)
            end = today
        elif kind == "rolling":
            count = int(re.search(r"\d+", phrase).group())
            if not 1 <= count <= 36600:
                raise ValueError("Relative date duration outside supported limits")
            unit = phrase.split()[-1].rstrip("s")
            end = today
            if unit in {"day", "week"}:
                start = today - timedelta(days=count * (7 if unit == "week" else 1) - 1)
            else:
                start = shift_months(today, -count * (12 if unit == "year" else 1))
        elif kind == "period":
            unit = phrase.split()[-1]
            current = bool(re.search(r"\bthis\b", phrase))
            if unit == "week":
                period_start = today - timedelta(days=today.weekday())
                start = period_start if current else period_start - timedelta(days=7)
            elif unit == "month":
                period_start = today.replace(day=1)
                start = period_start if current else shift_months(period_start, -1)
            else:
                period_start = today.replace(month=1, day=1)
                start = period_start if current else period_start.replace(year=today.year - 1)
            end = today if current else period_start - timedelta(days=1)
        else:
            end = today - timedelta(days=1) if phrase == "yesterday" else today
            start = today.replace(month=1, day=1) if phrase in {"ytd", "year-to-date"} else end
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid transaction date expression: {exc}") from exc
    if start is not None and end is not None and start > end:
        raise ValueError("Transaction dates are reversed")
    model_message = message[:match.start()] + "[resolved date filter]" + message[match.end():]
    return TransactionDates(
        start.isoformat() if start else None,
        end.isoformat() if end else None,
        model_message, remainder,
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


def parse_with_lmstudio(
    client: OpenAI, model_id: str, message: str, today: date | None = None,
) -> Route:
    today = today or date.today()
    resolved = None
    if TRANSACTION_RE.search(message):
        resolved = resolve_transaction_dates(message, today)
        message = resolved.model_message
    system_prompt = SYSTEM_PROMPT + f"\nToday's date is {today.isoformat()}."
    if resolved is not None:
        system_prompt += "\nTrusted transaction date bounds (copy exactly): " + json.dumps({
            "start_date": resolved.start_date, "end_date": resolved.end_date,
        })
    response = client.chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
        temperature=0,
        max_tokens=260,
    )
    content = response.choices[0].message.content or ""
    route = Route.model_validate(extract_json(content))
    if route.plan_id == "query_transactions" and resolved is not None:
        # Python owns date semantics, even if the model copies the bounds incorrectly.
        route.slots.start_date = resolved.start_date
        route.slots.end_date = resolved.end_date
    return route


def validate_route(
    message: str, route: Route, today: date | None = None,
) -> tuple[bool, str]:
    if route.plan_id not in PLANS or not route.fully_covered:
        return False, "No fully covered reviewed plan"
    if COMPLEX_RE.search(message):
        return False, "Potential additional or unsupported operation"

    slots = route.slots
    nums = [Decimal(x) for x in NUMBER_RE.findall(message)]
    if TRANSACTION_RE.search(message) and route.plan_id != "query_transactions":
        return False, "Transaction request must use the transaction plan"

    if route.plan_id == "get_balance":
        if nums or any(v is not None for v in slots.model_dump().values()):
            return False, "Balance-only plan must not contain numbers or slots"
        return True, ""

    if route.plan_id == "contact_information":
        if nums or any(v is not None for v in slots.model_dump().values()):
            return False, "Contact-information plan must not contain numbers or slots"
        if not CONTACT_RE.search(message):
            return False, "Contact-information wording not confirmed"
        return True, ""

    if route.plan_id == "query_transactions":
        if not TRANSACTION_RE.search(message):
            return False, "Transaction query wording not confirmed"

        if any(value is not None for value in (
            slots.amount, slots.frequency, slots.duration_value, slots.duration_unit,
        )):
            return False, "Unexpected expense slots in transaction query"
        try:
            expected = resolve_transaction_dates(message, today or date.today())
        except ValueError as exc:
            return False, str(exc)
        if (slots.start_date, slots.end_date) != (expected.start_date, expected.end_date):
            return False, "Transaction dates do not match request (missing or invented filter)"
        has_dates = slots.start_date is not None or slots.end_date is not None
        # Date components/durations must never validate an amount by coincidence.
        nums = [Decimal(x) for x in NUMBER_RE.findall(expected.non_date_message)]
        try:
            minimum = _validated_transaction_amount(slots.minimum_amount)
            maximum = _validated_transaction_amount(slots.maximum_amount)
        except ValueError as exc:
            return False, str(exc)
        has_amount_filter = minimum is not None or maximum is not None
        amount_text = expected.non_date_message
        amount_ranges = re.findall(
            r"\bbetween\s+\$?(\d+(?:\.\d+)?)\s+(?:and|to)\s+\$?(\d+(?:\.\d+)?)\b",
            amount_text, re.I,
        )
        amount_thresholds = re.findall(
            r"\b(?:over|above|more than|greater than)\s+\$?(\d+(?:\.\d+)?)\b",
            amount_text, re.I,
        )
        if len(amount_ranges) + len(amount_thresholds) > 1:
            return False, "Multiple amount expressions require fallback"
        if amount_ranges:
            requested_min, requested_max = map(Decimal, amount_ranges[0])
            if (minimum, maximum) != (requested_min, requested_max):
                return False, "Amount range was dropped or changed"
        elif amount_thresholds:
            if (minimum, maximum) != (Decimal(amount_thresholds[0]), None):
                return False, "Amount threshold was dropped or changed"
        elif has_amount_filter:
            return False, "Amount filter wording not confirmed"
        if re.search(r"\b(?:under|below|less than|at least|at most)\s+\$?\d", amount_text, re.I):
            return False, "Unsupported amount comparison requires fallback"
        if maximum is not None and minimum is None:
            return False, "Maximum amount requires a minimum amount"
        if minimum is not None:
            if minimum not in nums or (maximum is not None and maximum not in nums):
                return False, "Transaction amount does not match request"
            if maximum is None and not OVER_RE.search(amount_text):
                return False, "Amount-over wording not confirmed"
            if maximum is not None:
                if minimum > maximum:
                    return False, "Transaction amount range is reversed"
                if not BETWEEN_RE.search(amount_text):
                    return False, "Amount range wording not confirmed"

        if slots.store_name is None and re.search(
            r"\b(?:about|at|merchant|store(?:\s+named)?)\s+\w", amount_text, re.I,
        ):
            return False, "Store filter may have been dropped"
        if slots.store_name is not None:
            store_name = slots.store_name.strip()
            if not store_name or len(store_name) > 100 or store_name.casefold() not in message.casefold():
                return False, "Store name does not match request"
            if not re.search(
                rf"\b(?:about|at|merchant|store(?:\s+named)?)\s+{re.escape(store_name)}\b",
                message,
                re.I,
            ):
                return False, "Store filter wording not confirmed"

        if not (has_dates or has_amount_filter or slots.store_name):
            return False, "Transaction query requires a supported filter"
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
        if slots.frequency is not None or slots.duration_value is not None or slots.duration_unit is not None or any(
            value is not None for value in (
                slots.start_date, slots.end_date,
                slots.minimum_amount, slots.maximum_amount, slots.store_name,
            )
        ):
            return False, "Unexpected extra slots"
        if len(nums) != 1 or nums[0] != amount:
            return False, "Amount does not match unique number in request"
        if MONTHLY_RE.search(message):
            return False, "Recurring expense requires different plan"
        return True, ""

    if route.plan_id == "project_balance":
        if any(value is not None for value in (
            slots.start_date, slots.end_date,
            slots.minimum_amount, slots.maximum_amount, slots.store_name,
        )):
            return False, "Unexpected extra slots"
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


def _validated_transaction_amount(value: str | int | float | None) -> Decimal | None:
    if value is None:
        return None
    value_str = str(value)
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value_str):
        raise ValueError("Invalid transaction amount")
    amount = Decimal(value_str)
    if not amount.is_finite() or not Decimal("0") < amount <= Decimal("1000000"):
        raise ValueError("Transaction amount outside reviewed limits")
    return amount


def route_request(
    client: OpenAI, model_id: str, message: str, today: date | None = None,
) -> tuple[Route, float]:
    start = time.perf_counter()
    try:
        today = today or date.today()
        normalized_message = normalize_written_amounts(message)
        print(f"Normalized message: {normalized_message}", file=sys.stderr)
        proposal = parse_with_lmstudio(client, model_id, normalized_message, today)
        print(f"Proposed route: {proposal.model_dump()}", file=sys.stderr)
        valid, reason = validate_route(normalized_message, proposal, today)
        result = proposal if valid else unknown(reason)
    except (ValueError, ValidationError, KeyError, IndexError) as exc:
        result = unknown(f"Model output invalid: {type(exc).__name__}: {exc}")
    return result, (time.perf_counter() - start) * 1000


def preview(route: Route, balance: Decimal | None) -> dict:
    if route.plan_id == "unknown":
        return {"next": "pi_agent", "message": "Fallback to Pi Agent; no bank action executed"}
    if route.plan_id == "query_transactions":
        return {"next": "bank_tool", "plan": route.plan_id,
                "filters": route.slots.model_dump(exclude_none=True),
                "message": "Validated route only. Connect your read-only transaction tool in your application."}
    if route.plan_id == "contact_information":
        return {"next": "bank_tool", "plan": route.plan_id,
                "message": "Validated route only. Connect your public contact-information tool in your application."}
    if balance is None:
        return {"next": "bank_tool", "plan": route.plan_id,
                "message": "Validated route only. Connect your read-only balance tool in your application."}
    if route.plan_id == "get_balance":
        remaining = balance
    elif route.plan_id == "balance_after_spending":
        remaining = balance - Decimal(str(route.slots.amount))
    else:
        months = route.slots.duration_value * (12 if route.slots.duration_unit == "year" else 1)
        remaining = balance - Decimal(str(route.slots.amount)) * months
    return {"next": "hypothetical_preview", "starting_balance": str(balance),
            "remaining_balance": str(remaining), "note": "No bank transaction was performed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("message", nargs="?", help="One question; omit for interactive mode")
    parser.add_argument("--base-url", default=os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"))
    parser.add_argument("--model", default=os.getenv("LMSTUDIO_MODEL", "qwen3.5-0.8b-mlx"), help="Exact LM Studio model id")
    parser.add_argument("--balance", type=Decimal, help="Optional mock balance for hypothetical preview")
    parser.add_argument("--timezone", default=os.getenv("BANK_TIMEZONE"),
                        help="Business timezone, e.g. America/Toronto; default: system local timezone")
    parser.add_argument("--today", type=date.fromisoformat,
                        help="Override business date (YYYY-MM-DD) for reproducible testing")
    args = parser.parse_args()
    try:
        business_tz = ZoneInfo(args.timezone) if args.timezone else None
    except ZoneInfoNotFoundError:
        parser.error("Unknown timezone; install tzdata if system timezone data is unavailable")
    client = OpenAI(base_url=args.base_url, api_key="lm-studio", timeout=45.0, max_retries=0)
    try:
        model_id = choose_model(client, args.model)
    except Exception as exc:
        print(f"LM Studio setup error: {exc}", file=sys.stderr)
        return 1
    print(f"LM Studio: {args.base_url} | model: {model_id}", file=sys.stderr)

    def run(message: str) -> None:
        try:
            route, elapsed_ms = route_request(
                client, model_id, message,
                today=args.today or datetime.now(business_tz).date(),
            )
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
