"""One unfinished task per conversation, bound to a verified customer."""
import sqlite3
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from openai import OpenAIError
from pydantic import ValidationError

from workflows.models import TransferTask, TransferTurn, TransferDraft
from workflows.transfer import build_transfer_graph


class WorkflowManager:
    def __init__(self, gateway, store, parser, conversation_id):
        self.gateway, self.store, self.parser = gateway, store, parser
        self.conversation_id = conversation_id
        self.session = uuid4().hex
        self.connection = sqlite3.connect(store.path + ".checkpoints", check_same_thread=False)
        self.graph = build_transfer_graph(gateway, store, SqliteSaver(self.connection))

    def close(self):
        # Conversation end: cancel any unfinished task of this conversation so
        # no workflow is left dangling. In-flight submissions are untouched:
        # their money movement outcome is unknown, so they must not be marked
        # cancelled. Generic over workflow type; new workflows inherit this.
        with self.store.lock():
            task = self.store.load_any(self.conversation_id)
            if task and not task.terminal and task.status not in {"SUBMITTING", "RECONCILING"}:
                task.status = "CANCELLED"
                self.store.save(task)
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def get_task(self, owner):
        return self.store.load(self.conversation_id, owner)

    def reset(self, owner):
        with self.store.lock():
            task = self.get_task(owner)
            if task and not task.terminal:
                task.confirmation_session = None
                task.pending_question = None
                if task.status not in {"SUBMITTING", "RECONCILING"}:
                    task.status = "PAUSED"
                    task.confirmed_version = None
                self.store.save(task)
                return " Transfer draft retained. Say 'resume transfer' or 'cancel transfer'."
        return ""

    def handle(self, message, context, starting=False, turn=None):
        owner = context["customer_id"]
        with self.store.lock():
            task = self.get_task(owner)
            if task and task.confirmation_session != self.session:
                task.confirmation_session = None
            if turn is None:
                try:
                    turn = self.parser.parse(message, task)
                except (ValueError, ValidationError, OpenAIError, OSError, RuntimeError):
                    if task:
                        task.confirmation_session = None
                        self.store.save(task)
                    return "I could not safely interpret that. Please clarify the transfer details or requested action."
            if task is None or (task.terminal and turn.action == "start"):
                if turn.action not in {"start", "update"} or (not starting and turn.action != "start"):
                    return None if turn.action == "other" else "There is no active transfer. Please start a new transfer."
                changes = turn.changes.model_dump(exclude_unset=True)
                # Obtain a configured default from the server-owned account list.
                accounts = self.gateway.list_transfer_accounts(owner)
                default = accounts[0]["account_id"] if len(accounts) == 1 else None
                draft = TransferDraft.model_validate({"source_account_id": default, "currency": "CAD", **changes})
                task = TransferTask(owner=owner, conversation_id=self.conversation_id, draft=draft)
                turn = TransferTurn(action="update")
            state = self.graph.invoke({
                "task": task.model_dump(mode="json"),
                "turn": turn.model_dump(mode="json", exclude_unset=True),
                "session": self.session, "response": None, "handled": True, "next_node": "end",
            }, {"configurable": {"thread_id": task.task_id}})
            return state["response"] if state["handled"] else None
