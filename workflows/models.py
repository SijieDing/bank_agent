"""Untrusted turn proposals are separate from trusted workflow state."""
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransferDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: str | None = None
    recipient_email: str | None = None
    amount: Decimal | None = None
    currency: str | None = "CAD"

    @field_validator("amount", mode="before")
    @classmethod
    def money(cls, value):
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("Amount must be a number")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Amount must be a number") from exc
        if not amount.is_finite() or not 0 < amount <= 1000000 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Amount must be positive, at most 1000000, with at most two decimal places")
        return amount

    @field_validator("recipient_email")
    @classmethod
    def email(cls, value):
        if value is None:
            return value
        import re
        value = value.strip().lower()
        if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Provide a valid recipient email")
        return value

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        if value is not None and value.upper() != "CAD":
            raise ValueError("This demo supports CAD only")
        return value.upper() if value else None


class TransferTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "update", "pause", "resume", "cancel", "confirm", "other", "clarify"]
    # model_fields_set distinguishes omitted fields from explicit nulls.
    changes: TransferDraft = Field(default_factory=TransferDraft)


class ValidatedTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: str
    recipient_email: str
    recipient_id: str
    amount: Decimal
    currency: Literal["CAD"]


class TransferTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    owner: str
    conversation_id: str
    status: Literal["COLLECTING", "VALIDATING", "AWAITING_CONFIRMATION", "PAUSED", "SUBMITTING", "RECONCILING", "SUCCEEDED", "FAILED", "CANCELLED"] = "COLLECTING"
    draft: TransferDraft = Field(default_factory=TransferDraft)
    draft_version: int = 1
    confirmed_version: int | None = None
    confirmation_session: str | None = None
    pending_question: str | None = None
    validated: ValidatedTransfer | None = None
    submission_key: str | None = None
    result: dict | None = None

    @property
    def terminal(self):
        return self.status in {"SUCCEEDED", "FAILED", "CANCELLED"}
