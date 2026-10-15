"""Untrusted turn proposals are separate from trusted workflow state."""
from bank_contracts import TransferDraft, ValidatedTransfer
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransferTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "update", "pause", "resume", "cancel", "confirm", "other", "clarify"]
    # model_fields_set distinguishes omitted fields from explicit nulls.
    changes: TransferDraft = Field(default_factory=TransferDraft)


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
