# Minimal Banking Agent Framework

This project demonstrates a simple banking agent architecture with:

- Capability Router
- Small-model, validated fast path
- Pi fallback path
- Pi Agent for complex requests
- LLM-requested, local secure sign-in
- Banking Tool Gateway
- Three isolated mock customers and banking APIs

The fast path uses an OpenAI-compatible LM Studio server and a small local model
(by default `qwen3.5-0.8b-mlx`). Complex requests require the
[Pi CLI](https://pi.dev/docs/latest/cli) with a configured model and credentials.

## Architecture

```text
User
  |
  v
Unauthenticated --> Pi returns AUTHENTICATE/PUBLIC
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
  +--> Validated fast path --> Banking Tool Gateway
  |
  +--> Unknown/unsupported --> Pi Agent --> Banking Tool Gateway
```

## Requirements

- Python 3.10 or newer
- LM Studio with its local server running and the configured small model loaded
- Pi CLI installed and available as `pi` on `PATH`
- A configured Pi model and provider credentials

## Run

```bash
cd bank_agent
python3 -m pip install -r requirements.txt
python3 app.py
```

The router connects to `http://localhost:1234/v1` and uses
`qwen3.5-0.8b-mlx` by default. Override these with `LMSTUDIO_BASE_URL` and
`LMSTUDIO_MODEL`; set `BANK_TIMEZONE` (for example, `America/Toronto`) for
relative transaction dates. If the local router is unavailable or rejects a
request, the request falls back to Pi.

The app runs a continuous question-and-answer chat in your terminal. Type a
question and press Enter; the app answers and waits for your next question.
Use `/reset` to clear the conversation or `/exit` (also `/quit`, `exit`, or
`quit`) to stop. EOF or Ctrl-C also exits. Empty input is ignored, and a failed
request reports its error so you can try again.

The first personal banking question is classified by the local router and then
goes to Pi for the sign-in decision. A short model call decides whether sign-in
is needed by returning `AUTHENTICATE` or `PUBLIC`. `AUTHENTICATE` immediately
opens the hidden prompts; it does not depend on the model executing a tool.
`PUBLIC` continues to a general answer with no personal banking tools. An
invalid decision is retried once and then reported as an error. After successful
sign-in, validated balance, hypothetical spending, monthly projection, and
filtered transaction requests run without further Pi calls. Contact-information
and credit-card-summary fast paths are intentionally deferred; those requests
use the Pi fallback. Complex questions and contextual follow-ups also call Pi
and its read-only banking tools. You should see output similar to:

```text
You: What is my balance?
Agent: Please enter your card number and PIN in the hidden secure sign-in prompts.
Card number (hidden):
PIN (hidden):
Sign-in successful. This conversation is locked to this user.
Route: common, Capability: get_balance, Confidence: 1.00
Agent: Your available balance is CAD 5,320.45.
```

Requests outside the four reviewed plans, such as "Tell me last 3 transactions,"
use the Pi fallback.

## Secure sign-in and demo users

Card numbers and PINs are entered using `getpass` and checked locally in Python.
They are never included in model prompts, tool schemas/results, conversation
history, or subprocess arguments. The sign-in decision call has no tools and
receives only the chat message and safe conversation history. Before
verification, Pi cannot access any personal banking tool and the fast path is blocked.
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

The router asks the small local model to choose one reviewed plan and extract
its variables. Python then validates the complete request, amounts, dates, and
filters before any gateway call. A rejected known proposal falls back to Pi
with only that plan's corresponding tools; if that attempt fails for a signed-in
customer, Pi retries with the full tool set. Missing or unsupported proposals
use the full set immediately. Fast-path turns establish the corresponding
banking tool for a later follow-up. Pi uses the conversation to understand
fallback questions and fetches banking facts from the gateway.

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
date range. It supports `both` (the default), `saving`, and `purchase` filters,
amount and store filters, and an optional newest-first `limit`. Totals cover the
returned transactions. A production gateway must query complete account history
for the requested range.

Edit `customer_service_policy.md` to change how Pi answers complex questions.
The validated fast path uses fixed response templates in `common_intents.py`.
The policy guides Pi's output; verify important changes against sample customer
questions with a configured Pi model before relying on them.

Run the local regression and authentication checks with
`python3 -m unittest discover -v`. The fast-path tests use a fake LM Studio
response; the Pi tests substitute Pi responses and exercise the local HTTP tool
bridge without calling a model provider.
