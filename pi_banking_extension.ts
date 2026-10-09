import { Type, type TSchema } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  function register(name: string, description: string, parameters: TSchema) {
    pi.registerTool({
      name,
      label: name.replaceAll("_", " "),
      description,
      parameters,
      annotations: { readOnlyHint: true, destructiveHint: false },
      async execute(_id, args) {
        const response = await fetch(process.env.BANK_AGENT_URL!, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${process.env.BANK_AGENT_TOKEN}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ tool: name, ...args }),
        });
        const result = await response.text();
        if (!response.ok) throw new Error(result);
        return { content: [{ type: "text", text: result }], details: undefined };
      },
    });
  }

  register("get_balance", "Get the customer's available and current account balance.", Type.Object({}));
  register("search_transactions", "Search the customer's transactions by merchant.",
    Type.Object({ merchant: Type.Optional(Type.String()) }));
  register("get_transactions", "List transactions across all merchants, optionally by inclusive date range, transaction type, and newest-first limit. Use limit for requests like 'last 3 transactions'; no merchant or date is needed. Totals cover the returned records. Type defaults to both.",
    Type.Object({
      start_date: Type.Optional(Type.String({ description: "Inclusive YYYY-MM-DD start date" })),
      end_date: Type.Optional(Type.String({ description: "Inclusive YYYY-MM-DD end date" })),
      transaction_type: Type.Optional(Type.Union([Type.Literal("both"), Type.Literal("saving"), Type.Literal("purchase")])),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
    }));
  register("get_credit_card_summary", "Get the customer's credit card balance and payment details.", Type.Object({}));
  register("get_upcoming_payments", "Get upcoming payments for the customer's account.", Type.Object({}));
}
