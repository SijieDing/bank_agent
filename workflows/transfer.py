"""A turn-based LangGraph: END at each conversational wait, resume from stored state.

The task snapshot is authoritative for crash recovery. Graph checkpoints provide
execution history; no code automatically replays an old submission checkpoint.
"""
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from workflows.models import TransferTask, TransferDraft, TransferTurn


class GraphState(TypedDict):
    task: dict
    turn: dict
    session: str
    response: str | None
    handled: bool
    next_node: str


def build_transfer_graph(gateway, store, checkpointer):
    def persist(task, response, next_node="end", handled=True):
        store.save(task)
        return {"task": task.model_dump(mode="json"), "response": response,
                "next_node": next_node, "handled": handled}

    def apply(state):
        task = TransferTask.model_validate(state["task"])
        turn = TransferTurn.model_validate(state["turn"])
        changes = turn.changes.model_dump(exclude_unset=True)
        action = turn.action
        if task.status in {"SUBMITTING", "RECONCILING"}:
            return persist(task, None, "reconcile")
        if task.terminal:
            return persist(task, f"Transfer is {task.status.lower()}. Start a new transfer to create another.",
                           handled=action != "other")
        if action in {"other", "clarify"} and changes:
            action = "clarify"
        if action == "other":
            task.confirmation_session = None
            task.pending_question = None
            return persist(task, None, handled=False)
        if action == "clarify":
            task.confirmation_session = None
            return persist(task, "Please clarify whether you want to update, pause, resume, or cancel this transfer.")
        if action == "cancel":
            task.status = "CANCELLED"
            task.confirmation_session = None
            return persist(task, "Transfer cancelled. No money was moved.")
        if action == "pause":
            task.status = "PAUSED"
            task.confirmation_session = None
            task.confirmed_version = None
            return persist(task, "Transfer paused. Your draft is saved; say 'resume transfer' to continue.")
        if action == "start":
            task.confirmation_session = None
            return persist(task, "You already have an unfinished transfer. Resume or cancel it before starting another.")
        if task.status == "PAUSED" and action != "resume":
            return persist(task, "This transfer is paused. Resume it before changing or confirming it.")
        if action == "resume":
            task.confirmation_session = None
            task.confirmed_version = None
            task.status = "COLLECTING"
        if changes:
            task.draft = TransferDraft.model_validate({**task.draft.model_dump(), **changes})
            task.draft_version += 1
            task.confirmed_version = None
            task.confirmation_session = None
            task.validated = None
            task.status = "COLLECTING"
        elif action == "confirm" and task.status == "AWAITING_CONFIRMATION" and task.confirmation_session == state["session"]:
            task.confirmed_version = task.draft_version
            # Persist submission intent BEFORE the gateway can move money.
            task.submission_key = uuid4().hex
            task.status = "SUBMITTING"
            task.confirmation_session = None
            return persist(task, None, "submit")
        task.status = "VALIDATING"
        return persist(task, None, "validate")

    def validate(state):
        task = TransferTask.model_validate(state["task"])
        missing = [label for field, label in (
            ("amount", "amount"), ("recipient_email", "recipient email"),
            ("source_account_id", "source account"), ("currency", "currency"))
            if getattr(task.draft, field) is None]
        if missing:
            task.status = "COLLECTING"
            task.confirmation_session = None
            task.pending_question = "Please provide the " + ", ".join(missing) + "."
            return persist(task, task.pending_question)
        try:
            task.validated = gateway.validate_transfer(task.owner, task.draft)
        except ValueError as exc:
            task.status = "COLLECTING"
            task.validated = None
            task.confirmation_session = None
            task.pending_question = str(exc) + ". Please correct the transfer details."
            return persist(task, task.pending_question)
        task.status = "AWAITING_CONFIRMATION"
        task.confirmation_session = state["session"]
        task.pending_question = (
            f"Confirm transfer of {task.validated.currency} {task.validated.amount:,.2f} "
            f"from {task.validated.source_account_id} to {task.validated.recipient_email}. "
            "Mock fee: CAD 0.00. Submit this transfer?"
        )
        return persist(task, task.pending_question)

    def result(task, outcome):
        task.result = outcome
        task.status = outcome["status"]
        if task.status == "SUCCEEDED":
            message = f"Transfer successful. Transaction ID: {outcome['transaction_id']}."
        else:
            message = "Transfer failed: " + outcome.get("reason", "Rejected by backend") + ". No money was moved."
        return persist(task, message)

    def submit(state):
        task = TransferTask.model_validate(state["task"])
        if task.confirmed_version != task.draft_version or task.validated is None or not task.submission_key:
            raise ValueError("Transfer lacks confirmation for its current version")
        try:
            outcome = gateway.submit_transfer(task.owner, task.submission_key, task.validated)
        except ValueError as exc:
            return result(task, {"status": "FAILED", "reason": str(exc)})
        except (OSError, TimeoutError, RuntimeError):
            task.status = "RECONCILING"
            return persist(task, "Submission outcome is unknown. Ask to check the transfer status; do not submit a new transfer.")
        return result(task, outcome)

    def reconcile(state):
        task = TransferTask.model_validate(state["task"])
        try:
            outcome = gateway.get_transfer_status(task.owner, task.submission_key)
        except (OSError, TimeoutError, RuntimeError):
            outcome = None
        if outcome and outcome["status"] == "NOT_SUBMITTED":
            # Only a backend's definitive negative outcome permits this branch.
            if state["turn"]["action"] == "cancel":
                task.status = "CANCELLED"
                return persist(task, "Transfer cancelled. Backend confirmed no money was moved.")
            task.status = "SUBMITTING"
            return persist(task, None, "submit")
        if outcome is None:
            # Explicit resume can replay the SAME authorized idempotent request.
            # A missing REST result cannot establish that an earlier handler will not commit.
            if (state["turn"]["action"] == "resume"
                    and task.confirmed_version == task.draft_version
                    and task.submission_key and task.validated is not None):
                task.status = "SUBMITTING"
                return persist(task, None, "submit")
            # Status checks and cancellation never initiate a debit.
            task.status = "RECONCILING"
            return persist(task, "Transfer outcome is still unknown. No additional transfer was submitted; cancellation is not confirmed.")
        return result(task, outcome)

    graph = StateGraph(GraphState)
    graph.add_node("apply", apply)
    graph.add_node("validate", validate)
    graph.add_node("submit", submit)
    graph.add_node("reconcile", reconcile)
    graph.add_edge(START, "apply")
    graph.add_conditional_edges("apply", lambda state: state["next_node"],
                                {"validate": "validate", "submit": "submit", "reconcile": "reconcile", "end": END})
    graph.add_conditional_edges("reconcile", lambda state: state["next_node"],
                                {"submit": "submit", "end": END})
    for node in ("validate", "submit"):
        graph.add_edge(node, END)
    return graph.compile(checkpointer=checkpointer)
