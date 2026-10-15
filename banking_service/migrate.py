"""Offline migration of the earlier combined demo DB. Source is read-only."""
import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from bank_contracts import ValidatedTransfer
from banking_service.repository import BankRepository


def migrate(source, bank_target, workflow_target):
    source, bank_target, workflow_target = map(lambda p: Path(p).expanduser().resolve(),
                                              (source, bank_target, workflow_target))
    if not source.is_file():
        raise ValueError("Legacy database does not exist")
    if bank_target.exists() or workflow_target.exists() or bank_target == workflow_target:
        raise ValueError("Choose two distinct, nonexistent destination files")
    old = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        accounts = old.execute("SELECT account,owner,available,current FROM accounts").fetchall()
        transfers = old.execute("SELECT key,owner,payload,result FROM transfers").fetchall()
        tasks = old.execute("SELECT conversation,owner,data FROM tasks").fetchall()
    finally:
        old.close()
    bank = BankRepository(bank_target)
    with bank.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for account, owner, available, current in accounts:
            updated = db.execute("UPDATE accounts SET available=?,current=? WHERE account=? AND owner=?",
                                 (available, current, account, owner))
            if updated.rowcount != 1:
                raise ValueError("Legacy account does not match seed ownership")
        for key, owner, payload, result_json in transfers:
            transfer = ValidatedTransfer.model_validate_json(payload)
            transfer.amount = transfer.amount.quantize(Decimal("0.01"))
            db.execute("INSERT INTO transfers VALUES (?,?,?,?)",
                       (key, owner, transfer.model_dump_json(), result_json))
            result = json.loads(result_json)
            if result["status"] == "SUCCEEDED":
                tx = {"description": "Transfer to " + transfer.recipient_email,
                      "amount": float(transfer.amount), "date": result["date"],
                      "type": "purchase", "transaction_id": result["transaction_id"]}
                db.execute("INSERT INTO transactions VALUES (?,?,?)",
                           (result["transaction_id"], transfer.source_account_id, json.dumps(tx)))
    workflow_target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(workflow_target)
    try:
        with db:
            db.execute("CREATE TABLE tasks (conversation TEXT PRIMARY KEY, owner TEXT NOT NULL, data TEXT NOT NULL)")
            db.executemany("INSERT INTO tasks VALUES (?,?,?)", tasks)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--bank-db", required=True)
    parser.add_argument("--workflow-db", required=True)
    args = parser.parse_args()
    migrate(args.source, args.bank_db, args.workflow_db)
    print("Copied banking records and task snapshots. Original database was not modified.")


if __name__ == "__main__":
    main()
