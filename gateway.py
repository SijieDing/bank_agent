from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


class BankingToolGateway:
    """
    Provides stable banking capabilities to the agent.

    The agent should not know which backend system implements
    each banking operation.
    """

    def __init__(self):
        self.transfer_store = None

    def enable_transfers(self, store):
        self.transfer_store = store
        store.seed(self.ACCOUNTS)

    def list_transfer_accounts(self, owner):
        with self.transfer_store.connect() as db:
            rows = db.execute("SELECT account FROM accounts WHERE owner=?", (owner,)).fetchall()
        return [{"account_id": row[0], "currency": "CAD"} for row in rows]

    def resolve_transfer_recipient(self, email):
        recipients = {
            "dingsijie@gmail.com": "external-demo-001",
            "abc@example.com": "external-demo-002",
        }
        if email not in recipients:
            raise ValueError("Unknown recipient; use a registered mock recipient")
        return recipients[email]

    def validate_transfer(self, owner, draft, *, check_balance=True):
        from workflows.models import TransferDraft, ValidatedTransfer
        draft = TransferDraft.model_validate(draft.model_dump())
        if draft.source_account_id not in {a["account_id"] for a in self.list_transfer_accounts(owner)}:
            raise ValueError("Unauthorized source account")
        if draft.amount is None or draft.currency != "CAD":
            raise ValueError("Amount and CAD currency are required")
        recipient = self.resolve_transfer_recipient(draft.recipient_email)
        available, _ = self.transfer_store.balance(draft.source_account_id)
        if check_balance and Decimal(str(available)) < draft.amount:
            raise ValueError("Insufficient funds")
        return ValidatedTransfer(**draft.model_dump(), recipient_id=recipient)

    def submit_transfer(self, owner, key, transfer):
        from workflows.models import TransferDraft
        # Return a previously committed result before checking a now-reduced balance.
        existing = self.transfer_store.status(owner, key)
        if existing is not None:
            return self.transfer_store.submit(owner, key, transfer)
        validated = self.validate_transfer(owner, TransferDraft.model_validate(
            transfer.model_dump(exclude={"recipient_id"})), check_balance=False)
        if validated != transfer:
            raise ValueError("Recipient details changed; confirm again")
        return self.transfer_store.submit(owner, key, validated)

    def get_transfer_status(self, owner, key):
        # This synchronous local ledger can definitively establish non-submission.
        # A remote adapter must return None when it cannot establish the outcome.
        return self.transfer_store.status(owner, key) or {"status": "NOT_SUBMITTED"}

    # Keep the original demo account's history for existing callers.
    TRANSACTIONS = [
        {"description": "Costco", "amount": 128.32, "date": "2026-10-07", "type": "purchase"},
        {"description": "Rogers", "amount": 82.10, "date": "2026-10-06", "type": "purchase"},
        {"description": "Amazon", "amount": 49.99, "date": "2026-10-05", "type": "purchase"},
        {"description": "Savings transfer", "amount": 300.00, "date": "2026-10-01", "type": "saving"},
        {"description": "Grocery", "amount": 92.50, "date": "2026-09-15", "type": "purchase"},
        {"description": "Savings transfer", "amount": 200.00, "date": "2026-09-02", "type": "saving"},
        {"description": "Transit", "amount": 45.00, "date": "2026-08-20", "type": "purchase"},
        {"description": "Pharmacy", "amount": 23.75, "date": "2026-07-09", "type": "purchase"},
        {"description": "Savings transfer", "amount": 150.00, "date": "2026-07-08", "type": "saving"},
        {"description": "Coffee", "amount": 6.25, "date": "2026-06-30", "type": "purchase"},
    ]

    ACCOUNTS = {
        "CHK-001": {
            "available_balance": 5320.45,
            "current_balance": 5480.20,
            "transactions": TRANSACTIONS,
            "payments": [{"type": "Mortgage", "amount": 2100.00, "date": "2026-10-09"}],
        },
        "CHK-002": {
            "available_balance": 2486.10,
            "current_balance": 2536.10,
            "transactions": [
                {"description": "Metro", "amount": 74.20, "date": "2026-10-08", "type": "purchase"},
                {"description": "Savings transfer", "amount": 450.00, "date": "2026-10-02", "type": "saving"},
                {"description": "Bell", "amount": 65.00, "date": "2026-09-26", "type": "purchase"},
                {"description": "Savings transfer", "amount": 100.00, "date": "2026-08-04", "type": "saving"},
            ],
            "payments": [{"type": "Rent", "amount": 1450.00, "date": "2026-10-15"}],
        },
        "CHK-003": {
            "available_balance": 9175.80,
            "current_balance": 9420.30,
            "transactions": [
                {"description": "Home Depot", "amount": 215.40, "date": "2026-10-06", "type": "purchase"},
                {"description": "Savings transfer", "amount": 800.00, "date": "2026-10-03", "type": "saving"},
                {"description": "Pet store", "amount": 48.50, "date": "2026-09-21", "type": "purchase"},
                {"description": "Savings transfer", "amount": 600.00, "date": "2026-07-10", "type": "saving"},
            ],
            "payments": [{"type": "Car loan", "amount": 425.00, "date": "2026-10-20"}],
        },
    }

    CARDS = {
        "VISA-001": {"current_balance": 1842.31, "statement_balance": 1661.20, "minimum_payment": 85.00, "payment_due_date": "2026-10-18"},
        "VISA-002": {"current_balance": 620.75, "statement_balance": 580.50, "minimum_payment": 30.00, "payment_due_date": "2026-10-22"},
        "VISA-003": {"current_balance": 3210.90, "statement_balance": 2980.15, "minimum_payment": 150.00, "payment_due_date": "2026-10-25"},
    }

    def _account(self, account_id: str) -> dict[str, Any]:
        if not isinstance(account_id, str) or account_id not in self.ACCOUNTS:
            raise ValueError("Unknown account ID")
        return self.ACCOUNTS[account_id]

    def _transactions(self, account_id: str) -> list[dict[str, Any]]:
        rows = [dict(tx) for tx in self._account(account_id)["transactions"]]
        if self.transfer_store:
            for payload, result in self.transfer_store.transactions(account_id):
                rows.append({"description": "Transfer to " + payload["recipient_email"],
                             "amount": float(payload["amount"]), "date": result["date"],
                             "type": "purchase", "transaction_id": result["transaction_id"]})
        return sorted(rows, key=lambda tx: tx["date"], reverse=True)

    def get_balance(self, account_id: str) -> dict[str, Any]:
        account = dict(self._account(account_id))
        if self.transfer_store:
            account["available_balance"], account["current_balance"] = self.transfer_store.balance(account_id)
        return {
            "account_id": account_id,
            "available_balance": account["available_balance"],
            "current_balance": account["current_balance"],
            "currency": "CAD",
        }

    def get_recent_transactions(
        self,
        account_id: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "transactions": self._transactions(account_id)[:limit],
        }

    def search_transactions(
        self,
        account_id: str,
        merchant: str | None = None,
    ) -> dict[str, Any]:
        transactions = self._transactions(account_id)

        if merchant:
            transactions = [
                tx
                for tx in transactions
                if merchant.lower() in tx["description"].lower()
            ]

        return {
            "account_id": account_id,
            "transactions": transactions,
        }

    def get_transactions(
        self,
        account_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        transaction_type: str = "both",
        limit: int | None = None,
        minimum_amount: str | int | float | None = None,
        maximum_amount: str | int | float | None = None,
        store_name: str | None = None,
    ) -> dict[str, Any]:
        account_transactions = self._transactions(account_id)
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

    def get_credit_card_summary(self, card_id: str) -> dict[str, Any]:
        if not isinstance(card_id, str) or card_id not in self.CARDS:
            raise ValueError("Unknown card ID")
        return {
            "card_id": card_id,
            **self.CARDS[card_id],
            "currency": "CAD",
        }

    def get_upcoming_payments(self, account_id: str) -> dict[str, Any]:
        return {
            "account_id": account_id,
            "payments": [dict(payment) for payment in self._account(account_id)["payments"]],
        }

    def get_contact_information(self) -> dict[str, Any]:
        return {
            "phone": "+1-800-555-0142",
            "address": "123 Bank Street, Toronto, ON M5V 1J1",
            "hours": "Monday to Friday, 9:00 AM to 5:00 PM (Eastern)",
        }
