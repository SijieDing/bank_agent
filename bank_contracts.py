"""Shared REST data contracts; no database or agent runtime imports."""
from decimal import Decimal, InvalidOperation
from typing import Literal
from pydantic import BaseModel, ConfigDict, field_validator

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



class ValidatedTransfer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_account_id: str
    recipient_email: str
    recipient_id: str
    amount: Decimal
    currency: Literal["CAD"]
    fee: Decimal = Decimal("0.00")


