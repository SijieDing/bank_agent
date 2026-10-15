"""Bank-owned SQLite repository. All account access requires the session owner."""
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4
from typing import Any

from bank_contracts import TransferDraft, ValidatedTransfer


class BankRepository:
    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                raise ValueError("Agent task database detected: use a separate bank DB")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS customers (
                    card TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL,
                    default_account TEXT NOT NULL, default_card TEXT NOT NULL,
                    verifier TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    account TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    available INTEGER NOT NULL, current INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cards (
                    card TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS payments (
                    account TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recipients (email TEXT PRIMARY KEY, id TEXT UNIQUE NOT NULL);
                CREATE TABLE IF NOT EXISTS contact (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transfers (
                    key TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
                from banking_service.seed import ACCOUNTS, CARDS, CUSTOMERS, RECIPIENTS, CONTACT
                for card, (owner, account, card_id, verifier) in CUSTOMERS.items():
                    data = ACCOUNTS[account]
                    db.execute("INSERT INTO customers VALUES (?,?,?,?,?)", (card, owner, account, card_id, verifier))
                    db.execute("INSERT INTO accounts VALUES (?,?,?,?)",
                               (account, owner, int(Decimal(str(data["available_balance"])) * 100),
                                int(Decimal(str(data["current_balance"])) * 100)))
                    db.execute("INSERT INTO cards VALUES (?,?,?)", (card_id, owner, json.dumps(CARDS[card_id])))
                    db.execute("INSERT INTO payments VALUES (?,?)", (account, json.dumps(data["payments"])))
                    for index, tx in enumerate(data["transactions"]):
                        db.execute("INSERT INTO transactions VALUES (?,?,?)",
                                   (f"seed-{account}-{index}", account, json.dumps(tx)))
                db.executemany("INSERT INTO recipients VALUES (?,?)", RECIPIENTS.items())
                db.execute("INSERT INTO contact VALUES (1,?)", (json.dumps(CONTACT),))
                db.execute("INSERT INTO metadata VALUES ('seeded','1')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def authenticate(self, card_number, pin):
        if not isinstance(card_number, str) or not isinstance(pin, str):
            return None
        if len(card_number) > 64 or not re.fullmatch(r"[0-9 -]+", card_number):
            return None
        if not re.fullmatch(r"[0-9]{4,6}", pin):
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM customers WHERE card=?",
                             (card_number.replace(" ", "").replace("-", ""),)).fetchone()
        if row is None:
            return None
        verifier = hashlib.pbkdf2_hmac("sha256", pin.encode(),
                                      ("bank-demo:" + row["owner"]).encode(), 100_000).hex()
        if not hmac.compare_digest(verifier, row["verifier"]):
            return None
        return {"customer_id": row["owner"], "default_account_id": row["default_account"],
                "default_card_id": row["default_card"]}

    def _account(self, owner, account_id, db):
        row = db.execute("SELECT * FROM accounts WHERE account=? AND owner=?", (account_id, owner)).fetchone()
        if row is None:
            raise PermissionError("Unauthorized source account")
        return row

    def list_accounts(self, owner):
        with self.connect() as db:
            rows = db.execute("SELECT account FROM accounts WHERE owner=? ORDER BY account", (owner,)).fetchall()
        return [{"account_id": row["account"], "currency": "CAD"} for row in rows]

    def get_balance(self, owner, account_id):
        with self.connect() as db:
            row = self._account(owner, account_id, db)
        return {"account_id": account_id, "available_balance": row["available"] / 100,
                "current_balance": row["current"] / 100, "currency": "CAD"}

    def _transactions(self, owner, account_id):
        with self.connect() as db:
            self._account(owner, account_id, db)
            rows = db.execute("SELECT data FROM transactions WHERE account=?", (account_id,)).fetchall()
        return sorted((json.loads(row["data"]) for row in rows), key=lambda tx: tx["date"], reverse=True)

    def get_credit_card_summary(self, owner, card_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM cards WHERE card=? AND owner=?", (card_id, owner)).fetchone()
        if row is None:
            raise PermissionError("Unknown or unauthorized card")
        return {"card_id": card_id, **json.loads(row["data"]), "currency": "CAD"}

    def get_upcoming_payments(self, owner, account_id):
        with self.connect() as db:
            self._account(owner, account_id, db)
            row = db.execute("SELECT data FROM payments WHERE account=?", (account_id,)).fetchone()
        return {"account_id": account_id, "payments": json.loads(row["data"]) if row else []}

    def get_contact_information(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM contact WHERE id=1").fetchone()
        return json.loads(row["data"])

    def _validate_transfer(self, owner, draft, db, check_balance=True):
        if draft.amount is None or draft.recipient_email is None or draft.currency != "CAD":
            raise ValueError("Amount, recipient and CAD currency are required")
        row = self._account(owner, draft.source_account_id, db)
        recipient = db.execute("SELECT id FROM recipients WHERE email=?", (draft.recipient_email,)).fetchone()
        if recipient is None:
            raise ValueError("Unknown recipient; use a registered mock recipient")
        if check_balance and row["available"] < int(draft.amount * 100):
            raise ValueError("Insufficient funds")
        fee = (draft.amount * Decimal("0.01")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ValidatedTransfer(**draft.model_dump(), recipient_id=recipient["id"], fee=fee)

    def validate_transfer(self, owner, draft):
        with self.connect() as db:
            return self._validate_transfer(owner, draft, db)

    def submit_transfer(self, owner, key, draft):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
            raise ValueError("A valid Idempotency-Key header is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Revalidate syntax/ownership on the server, not the agent.
            transfer = self._validate_transfer(owner, draft, db, check_balance=False)
            transfer.amount = transfer.amount.quantize(Decimal("0.01"))
            payload = transfer.model_dump_json()
            previous = db.execute("SELECT * FROM transfers WHERE key=?", (key,)).fetchone()
            if previous:
                if previous["owner"] != owner or previous["payload"] != payload:
                    raise ValueError("Idempotency key cannot be reused with different transfer details")
                return json.loads(previous["result"])
            cents = int(transfer.amount * 100)
            row = self._account(owner, transfer.source_account_id, db)
            if row["available"] < cents:
                result = {"status": "FAILED", "reason": "Insufficient funds"}
            else:
                db.execute("UPDATE accounts SET available=available-?,current=current-? WHERE account=?",
                           (cents, cents, transfer.source_account_id))
                result = {"status": "SUCCEEDED", "transaction_id": uuid4().hex,
                          "amount": str(transfer.amount), "recipient_email": transfer.recipient_email,
                          "currency": transfer.currency, "date": date.today().isoformat()}
                tx = {"description": "Transfer to " + transfer.recipient_email,
                      "amount": float(transfer.amount), "date": result["date"],
                      "type": "purchase", "transaction_id": result["transaction_id"]}
                db.execute("INSERT INTO transactions VALUES (?,?,?)",
                           (result["transaction_id"], transfer.source_account_id, json.dumps(tx)))
            db.execute("INSERT INTO transfers VALUES (?,?,?,?)", (key, owner, payload, json.dumps(result)))
        return result

    def get_transfer_status(self, owner, key):
        with self.connect() as db:
            # No record is not proof of non-submission: another HTTP handler may be queued.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,result FROM transfers WHERE key=?", (key,)).fetchone()
        if row and row["owner"] != owner:
            raise PermissionError("Unknown or unauthorized transaction")
        return json.loads(row["result"]) if row else None

    def get_transactions(
        self,
        owner: str,
        account_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        transaction_type: str = "both",
        limit: int | None = None,
        minimum_amount: str | int | float | None = None,
        maximum_amount: str | int | float | None = None,
        store_name: str | None = None,
    ) -> dict[str, Any]:
        account_transactions = self._transactions(owner, account_id)
        if transaction_type not in ("both", "saving", "purchase"):
            raise ValueError("transaction_type must be both, saving, or purchase")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("limit must be an integer from 1 to 100")
        if start_date is not None and (
            not isinstance(start_date, str) or not start_date
        ):
            raise ValueError("start_date must be an ISO date")
        if end_date is not None and (
            not isinstance(end_date, str) or not end_date
        ):
            raise ValueError("end_date must be an ISO date")
        try:
            minimum = Decimal(str(minimum_amount)) if minimum_amount is not None else None
            maximum = Decimal(str(maximum_amount)) if maximum_amount is not None else None
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Transaction amounts must be numbers") from exc
        if minimum is not None and (
            not minimum.is_finite() or not Decimal("0") < minimum <= Decimal("1000000")
        ):
            raise ValueError("minimum_amount must be between 0 and 1000000")
        if maximum is not None and (
            not maximum.is_finite() or not Decimal("0") < maximum <= Decimal("1000000")
        ):
            raise ValueError("maximum_amount must be between 0 and 1000000")
        if maximum is not None and minimum is None:
            raise ValueError("maximum_amount requires minimum_amount")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum_amount must not exceed maximum_amount")
        if store_name is not None and (not isinstance(store_name, str) or not store_name.strip()):
            raise ValueError("store_name must be a non-empty string")
        try:
            start = date.fromisoformat(start_date) if start_date else None
            end = date.fromisoformat(end_date) if end_date else None
        except ValueError as exc:
            raise ValueError("Dates must use YYYY-MM-DD") from exc
        if (start and start.isoformat() != start_date) or (
            end and end.isoformat() != end_date
        ):
            raise ValueError("Dates must use YYYY-MM-DD")
        if start and end and start > end:
            raise ValueError("start_date must be on or before end_date")

        in_range = []
        for tx in account_transactions:
            tx_date = date.fromisoformat(tx["date"])
            if (start is None or tx_date >= start) and (end is None or tx_date <= end):
                in_range.append(tx)
        transactions = sorted((
            tx for tx in in_range
            if transaction_type == "both" or tx["type"] == transaction_type
        ), key=lambda tx: tx["date"], reverse=True)
        if minimum is not None:
            comparison = (
                (lambda amount: amount >= minimum)
                if maximum is not None
                else (lambda amount: amount > minimum)
            )
            transactions = [
                tx for tx in transactions if comparison(Decimal(str(tx["amount"])))
            ]
        if maximum is not None:
            transactions = [
                tx for tx in transactions if Decimal(str(tx["amount"])) <= maximum
            ]
        if store_name is not None:
            transactions = [
                tx for tx in transactions
                if store_name.casefold() in tx["description"].casefold()
            ]
        if limit is not None:
            transactions = transactions[:limit]

        def total(kind: str) -> str:
            amount = sum(
                (Decimal(str(tx["amount"])) for tx in transactions if tx["type"] == kind),
                Decimal("0"),
            )
            return str(amount.quantize(Decimal("0.01")))

        return {
            "account_id": account_id,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "transaction_type": transaction_type,
            "limit": limit,
            "minimum_amount": str(minimum) if minimum is not None else None,
            "maximum_amount": str(maximum) if maximum is not None else None,
            "store_name": store_name,
            "transactions": transactions,
            "total_saving": total("saving"),
            "total_purchase": total("purchase"),
            "currency": "CAD",
        }

