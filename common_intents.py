from gateway import BankingToolGateway


class CommonIntentHandler:
    def __init__(self, gateway: BankingToolGateway):
        self.gateway = gateway

    def handle(
        self,
        capability: str,
        customer_context: dict | None,
    ) -> str:
        if capability == "contact_information":
            data = self.gateway.get_contact_information()

            return (
                f"Call us at {data['phone']}. "
                f"Our office is at {data['address']}, open {data['hours']}."
            )

        account_id = customer_context["default_account_id"]

        if capability == "account_balance":
            data = self.gateway.get_balance(account_id)

            return (
                f"Your available balance is "
                f"{data['currency']} "
                f"{data['available_balance']:,.2f}."
            )

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
