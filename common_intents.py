from decimal import Decimal

from bank_fast_path_router import Slots
from gateway import BankingToolGateway


class CommonIntentHandler:
    def __init__(self, gateway: BankingToolGateway):
        self.gateway = gateway

    def handle(
        self,
        capability: str,
        customer_context: dict | None,
        slots: Slots | None = None,
    ) -> str:
        if capability == "contact_information":
            data = self.gateway.get_contact_information()

            return (
                f"Call us at {data['phone']}. "
                f"Our office is at {data['address']}, open {data['hours']}."
            )

        account_id = customer_context["default_account_id"]

        if capability in ("account_balance", "get_balance"):
            data = self.gateway.get_balance(account_id)

            return (
                f"Your available balance is "
                f"{data['currency']} "
                f"{data['available_balance']:,.2f}."
            )

        if capability == "balance_after_spending":
            if slots is None or slots.amount is None:
                raise ValueError("balance_after_spending requires an amount")
            data = self.gateway.get_balance(account_id)
            amount = Decimal(str(slots.amount))
            remaining = Decimal(str(data["available_balance"])) - amount
            return (
                f"After spending {data['currency']} {amount:,.2f}, "
                f"your available balance would be {data['currency']} {remaining:,.2f}."
            )

        if capability == "project_balance":
            if (
                slots is None
                or slots.amount is None
                or slots.duration_value is None
                or slots.duration_unit is None
            ):
                raise ValueError("project_balance requires an amount and duration")
            data = self.gateway.get_balance(account_id)
            months = slots.duration_value * (12 if slots.duration_unit == "year" else 1)
            amount = Decimal(str(slots.amount))
            total = amount * months
            remaining = Decimal(str(data["available_balance"])) - total
            return (
                f"After {months} monthly expenses of "
                f"{data['currency']} {amount:,.2f}, "
                f"your available balance would be {data['currency']} {remaining:,.2f}."
            )

        if capability == "query_transactions":
            if slots is None:
                raise ValueError("query_transactions requires filters")
            data = self.gateway.get_transactions(
                account_id,
                start_date=slots.start_date,
                end_date=slots.end_date,
                minimum_amount=slots.minimum_amount,
                maximum_amount=slots.maximum_amount,
                store_name=slots.store_name.strip() if slots.store_name else None,
            )
            if not data["transactions"]:
                return "No matching transactions were found."
            lines = [
                f"{tx['date']} - {tx['description']} ({tx['type']}): "
                f"${tx['amount']:.2f}"
                for tx in data["transactions"]
            ]
            total = Decimal(data["total_saving"]) + Decimal(data["total_purchase"])
            return (
                f"The total of your matching transactions is "
                f"{data['currency']} {total:,.2f}.\n"
                "Your matching transactions are:\n" + "\n".join(lines)
            )

        if capability == "latest_transaction":
            limit = slots.limit if slots and slots.limit is not None else 1
            data = self.gateway.get_transactions(account_id, limit=limit)
            if not data["transactions"]:
                return "No transactions were found."
            lines = [
                f"{tx['date']} - {tx['description']} ({tx['type']}): "
                f"${tx['amount']:.2f}"
                for tx in data["transactions"]
            ]
            if limit == 1:
                return "Your latest transaction is " + lines[0] + "."
            return "Your latest transactions are:\n" + "\n".join(lines)

        if capability == "recent_transactions":
            data = self.gateway.get_recent_transactions(account_id)

            lines = [
                f"{tx['date']} - {tx['description']} ({tx['type']}): "
                f"${tx['amount']:.2f}"
                for tx in data["transactions"]
            ]

            return "Your recent transactions are:\n" + "\n".join(lines)

        if capability == "credit_card_summary":
            card_id = customer_context["default_card_id"]
            data = self.gateway.get_credit_card_summary(card_id)

            return (
                f"Your credit card balance is "
                f"{data['currency']} "
                f"{data['current_balance']:,.2f}. "
                f"The minimum payment is "
                f"${data['minimum_payment']:.2f}, "
                f"due on {data['payment_due_date']}."
            )

        raise ValueError(
            f"Unsupported common capability: {capability}"
        )
