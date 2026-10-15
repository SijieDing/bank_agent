"""Agent task snapshots only. Banking data lives behind the REST service."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from workflows.models import TransferTask


class WorkflowStore:
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
                raise ValueError("Bank ledger detected: use a separate workflow DB; see banking_service.migrate")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tasks (
                    conversation TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
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

