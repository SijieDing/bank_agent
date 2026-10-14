"""Durable demo ledger and task snapshots; no credentials or login sessions."""
import json
from datetime import date
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from workflows.models import TransferTask


class WorkflowStore:
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    conversation TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    available INTEGER NOT NULL, current INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers (
                    key TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                    result TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self):
        # Cross-process serialization of workflow turns on macOS/Linux.
        # Ledger submission also uses its own SQLite transaction.
        import fcntl
        with open(self.path + ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self, conversation, owner):
        with self.connect() as db:
            row = db.execute("SELECT owner,data FROM tasks WHERE conversation=?", (conversation,)).fetchone()
        if row is None:
            return None
        if row[0] != owner:
            raise ValueError("This conversation belongs to another customer; start a new conversation")
        return TransferTask.model_validate_json(row[1])

    def save(self, task):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner FROM tasks WHERE conversation=?", (task.conversation_id,)).fetchone()
            if row and row[0] != task.owner:
                raise ValueError("Task owner cannot change")
            db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?)",
                       (task.conversation_id, task.owner, task.model_dump_json()))

    def seed(self, accounts):
        with self.connect() as db:
            for index, (account, data) in enumerate(accounts.items(), 1):
                from decimal import Decimal
                db.execute("INSERT OR IGNORE INTO accounts VALUES (?,?,?,?)", (
                    account, f"C1000{index}",
                    int(Decimal(str(data["available_balance"])) * 100),
                    int(Decimal(str(data["current_balance"])) * 100)))

    def balance(self, account):
        with self.connect() as db:
            row = db.execute("SELECT available,current FROM accounts WHERE account=?", (account,)).fetchone()
        if row is None:
            raise ValueError("Unknown account")
        return row[0] / 100, row[1] / 100

    def status(self, owner, key):
        with self.connect() as db:
            row = db.execute("SELECT owner,result FROM transfers WHERE key=?", (key,)).fetchone()
        if row and row[0] != owner:
            raise ValueError("Unauthorized transaction")
        return json.loads(row[1]) if row else None

    def submit(self, owner, key, transfer):
        from uuid import uuid4
        payload = transfer.model_dump_json()
        cents = int(transfer.amount * 100)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT owner,payload,result FROM transfers WHERE key=?", (key,)).fetchone()
            if existing:
                if existing[0] != owner or existing[1] != payload:
                    raise ValueError("Idempotency key cannot be reused with different transfer details")
                return json.loads(existing[2])
            row = db.execute("SELECT owner,available FROM accounts WHERE account=?", (transfer.source_account_id,)).fetchone()
            if not row or row[0] != owner:
                raise ValueError("Unauthorized source account")
            if row[1] < cents:
                result = {"status": "FAILED", "reason": "Insufficient funds"}
            else:
                db.execute("UPDATE accounts SET available=available-?,current=current-? WHERE account=?",
                           (cents, cents, transfer.source_account_id))
                # External recipients use a durable settlement record in this mock.
                result = {"status": "SUCCEEDED", "transaction_id": uuid4().hex,
                          "amount": str(transfer.amount), "recipient_email": transfer.recipient_email,
                          "currency": transfer.currency, "date": date.today().isoformat()}
            db.execute("INSERT INTO transfers VALUES (?,?,?,?)", (key, owner, payload, json.dumps(result)))
            return result

    def transactions(self, account):
        with self.connect() as db:
            rows = db.execute("SELECT payload,result FROM transfers").fetchall()
        output = []
        for payload, result in rows:
            payload, result = json.loads(payload), json.loads(result)
            if payload["source_account_id"] == account and result["status"] == "SUCCEEDED":
                output.append((payload, result))
        return output
