# Minimal Banking Agent Framework

This project demonstrates a simple banking agent architecture with:

- Capability Router
- Common Intent fast path
- Complex Intent path
- Pi Agent for complex requests
- LLM-requested, local secure sign-in
- Banking Tool Gateway
- Three isolated mock customers and banking APIs

The Python app has no third-party dependencies. Complex requests require the
[Pi CLI](https://pi.dev/docs/latest/cli) with a configured model and credentials.

## Architecture

```text
User
  |
  v
Unauthenticated --> Public contact tool, or Pi returns AUTHENTICATE/PUBLIC
                          |
                          v
                 Hidden local card/PIN prompts
                          |
                          v
                 Local verification, fixed customer
                          |
                          v
Capability Router
  |
  +--> Common Intent --> Banking Tool Gateway
  |
  +--> Complex Intent --> Pi Agent --> Banking Tool Gateway
```

## Requirements

- Python 3.10 or newer
- Pi CLI installed and available as `pi` on `PATH`
- A configured Pi model and provider credentials

## Run

```bash
cd bank_agent
python3 app.py
```

The app runs a continuous question-and-answer chat in your terminal. Type a
question and press Enter; the app answers and waits for your next question.
Use `/reset` to clear the conversation or `/exit` (also `/quit`, `exit`, or
`quit`) to stop. EOF or Ctrl-C also exits. Empty input is ignored, and a failed
request reports its error so you can try again.

The first personal banking question goes to Pi, including common balance
questions. A short model call decides whether sign-in is needed by returning
`AUTHENTICATE` or `PUBLIC`. `AUTHENTICATE` immediately opens the hidden prompts;
it does not depend on the model executing a tool. `PUBLIC` continues to a general
answer with no personal banking tools. Contact requests can use the public
`get_contact_information` gateway tool without signing in, including phrasing
that misses the common-intent fast path. An invalid decision is retried once and then
reported as an error. After successful sign-in, the common path works without further Pi calls.
Complex questions and contextual follow-ups call Pi and its read only banking
tools. You should see output similar to:

```text
You: What is my balance?
Agent: Please enter your card number and PIN in the hidden secure sign-in prompts.
Card number (hidden):
PIN (hidden):
Sign-in successful. This conversation is locked to this user.
Route: common, Capability: account_balance, Confidence: 0.98
Agent: Your available balance is CAD 5,320.45.
```

Requests such as "Tell me last 3 transactions" use Pi with only the
`get_transactions` tool and set its limit to the requested count.

## Secure sign-in and demo users

Card numbers and PINs are entered using `getpass` and checked locally in Python.
They are never included in model prompts, tool schemas/results, conversation
history, or subprocess arguments. The sign-in decision call has no tools and
receives only the chat message and safe conversation history. Before
verification, Pi cannot access any banking tool and the common path is blocked.
On success, the app answers the original question using the verified customer.
Pi's input stream is disconnected from the terminal or pipe, so it cannot
consume later chat messages or secure input. The app reads Pi's structured JSON
events to distinguish assistant text from tool results, thinking, and errors.

Use one of these public, fake credentials in the secure prompts:

| User | Card number | PIN | Account | Card |
| --- | --- | --- | --- | --- |
| 1 | `4111111111111111` | `1234` | `CHK-001` | `VISA-001` |
| 2 | `5555555555554444` | `2345` | `CHK-002` | `VISA-002` |
| 3 | `4000000000000002` | `3456` | `CHK-003` | `VISA-003` |

Each user has separate balances, transactions, card details, and payments. PINs
are checked against local PBKDF2 verifiers. These public fixtures are for this
demo; real authentication requires a bank identity service.

Three invalid card/PIN pairs end the conversation. The user cannot change
identity or sign in again during the conversation. `/reset` clears messages
while keeping the verified user and failed-attempt count; it cannot reopen an
ended conversation. Start a new app instance to begin a separate conversation.
The attempt limit applies to this conversation, not across restarts.

Do not paste credentials into chat. A local guard rejects ordinary card/PIN
patterns before routing, history, or Pi, including direct Pi API calls. This
guard is conservative and may reject unrelated numeric identifiers; it cannot
recognize every encoding of a secret. The dedicated local prompts provide the
credential isolation.

## Conversation context

Each `BankingAgent` keeps the latest 10 completed question-and-answer pairs in
memory, including common-path answers. Earlier pairs drop out as the chat
continues. The app sends these pairs as labeled JSON context with each Pi
request; Pi itself still runs with `--no-session`. Failed requests do not enter
the history.

The router selects tools from the current question. If it selects none, the app
reuses the previous successful turn's tools, allowing follow-ups such as
"What about Amazon?" after a spending question. Common-path turns also establish
the corresponding banking tool for a later follow-up. An initial unrecognized
request receives no banking tools. Pi uses the conversation to understand the
question and fetches banking facts from the gateway.

`/reset` clears message history and inherited tools. Customer identity comes
only from successful local verification, and cannot change through chat or a
caller-supplied context. `BankingAgent.handle(message)` takes no customer context;
`authenticate(card_number, pin)` is allowed only after Pi requests sign-in.
There is no saved conversation or restart recovery.

## Files

- `app.py`: Application entry point
- `authentication.py`: Local demo verification and chat credential guard
- `router.py`: Capability Router
- `common_intents.py`: Fast path for common requests
- `pi_agent.py`: Pi CLI integration and temporary gateway bridge
- `pi_banking_extension.ts`: Pi banking tools
- `customer_service_policy.md`: Pi response pattern, tone, and banking rules
- `gateway.py`: Banking Tool Gateway and mock banking APIs

The gateway still returns mock banking data. Pi only receives access to the
current customer's read only gateway methods. Tool access comes from the current
route, or the previous successful turn when the current route has no tools.
Pi's file and shell tools are disabled.

`get_transactions` lists matching mock transactions within an optional inclusive
date range. It supports `both` (the default), `saving`, and `purchase` filters
and an optional newest-first `limit`. Totals cover the returned transactions.
A production gateway
must query complete account history for the requested range.

Edit `customer_service_policy.md` to change how Pi answers complex questions.
The common intent path uses fixed response templates in `common_intents.py`.
The policy guides Pi's output; verify important changes against sample customer
questions with a configured Pi model before relying on them.

Run the local regression and authentication checks with
`python3 -m unittest discover -v`. The tests substitute Pi responses and exercise
the local HTTP tool bridge without calling a model provider.
