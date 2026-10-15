# Banking agent with a separate mock REST backend

The terminal agent uses a small local model for validated fast paths, Pi for
complex read-only questions, and LangGraph/Pydantic for multi-turn transfers.
**All banking data is read and written through REST APIs.**

## Architecture

- Fast path / transfer workflow → BankingToolGateway HTTP client → banking REST service → bank SQLite database.
- Pi extension → restricted local tool bridge → the same HTTP client → the same banking REST service.
- Agent workflow → separate task/checkpoint store containing drafts and workflow state only.

The agent never imports the bank repository, opens the bank database, or performs
SQL against balances/transactions. The gateway has no fixtures or database access.
The TypeScript Pi extension retains its local bridge, which binds tool calls to
the signed-in account and calls the REST gateway. Pi still has only read tools.

## Run

Requires Python 3.10+, LM Studio with its configured model loaded, and Pi CLI
configured for complex questions.

Install:

    python -m pip install -r requirements.txt

Start the bank service in a separate terminal:

    python -m banking_service.server

Start the agent:

    python app.py

| Setting | Default | Owner |
| --- | --- | --- |
| BANK_API_URL | http://127.0.0.1:8765 | Agent HTTP client |
| BANK_DB | ~/.bank_agent/bank.sqlite3 | Banking service only |
| BANK_WORKFLOW_DB | ~/.bank_agent/workflows.sqlite3 | Agent task store |
| BANK_CONVERSATION_ID | Generated and printed at startup | Agent |
| LMSTUDIO_BASE_URL | http://localhost:1234/v1 | Small-model router |
| LMSTUDIO_MODEL | qwen3.5-0.8b-mlx | Small-model router |
| BANK_TIMEZONE | Existing system default | Relative query dates |

The service accepts --db and --port overrides and binds only to 127.0.0.1. The
agent does not automatically start a bank service or fall back to local data.
Non-loopback client URLs require HTTPS; this server is a local mock, not a
production HTTP deployment.

## Authentication and isolation

Card/PIN prompts remain hidden. Credentials go directly to the bank's /auth
endpoint, never to the model, Pi arguments, tools, chat history, or checkpoints.
The service verifies PBKDF2 hashes stored in its customers table and returns an
opaque session token plus safe customer context.

The token stays privately in the gateway. The service authorizes every personal
request using that token. Request fields cannot override customer identity;
accounts, cards and transfer status belonging to another customer are rejected.
The Pi bridge token and bank session token are separate. Pi never receives the
bank token.

Sessions expire after one hour, are revoked on normal agent shutdown, and do not
survive bank-service restart. Restart the agent and sign in again if its bank
session expires. Restoring a workflow never restores authentication.

Public fake credentials:

| Customer | Card number | PIN | Account | Card |
| --- | --- | --- | --- | --- |
| 1 | 4111111111111111 | 1234 | CHK-001 | VISA-001 |
| 2 | 5555555555554444 | 2345 | CHK-002 | VISA-002 |
| 3 | 4000000000000002 | 3456 | CHK-003 | VISA-003 |

Three invalid attempts end the current conversation. /reset does not change
identity or reset failed attempts. The credential guard rejects card/PIN patterns
before model calls. For bare four-to-six-digit amounts, type "1000 dollars"
instead of "1000". Authentication is bank-owned; authentication.py retains the
chat guard and an API-backed compatibility helper, not a customer database.

## REST API

| Method and path | Purpose |
| --- | --- |
| POST /auth | Verify card/PIN and issue a session |
| POST /logout | Revoke the current session |
| GET /contact | Public contact information |
| GET /accounts | List signed-in customer's accounts |
| GET /accounts/{id}/balance | Current and available balance |
| GET /accounts/{id}/transactions | Date, amount, store, type and limit filters |
| GET /accounts/{id}/payments | Upcoming payments |
| GET /cards/{id}/summary | Credit-card summary |
| POST /transfers/validate | Validate draft and resolve recipient |
| POST /transfers | Submit with Idempotency-Key header |
| GET /transfers/by-key/{key} | Reconcile submission outcome |

Except /auth and /contact, requests require Authorization: Bearer <token>.
Account ownership, recipients, currency, amount, funds and idempotency are
enforced by the backend, even when the workflow already validated the draft.
Transfer JSON contains source_account_id, recipient_email, amount and currency.
Caller-supplied owner or resolved recipient ID fields are rejected.

## Bank storage

banking_service/seed.py contains initialization fixtures only. A fresh bank DB
is seeded once transactionally. Restarts do not overwrite existing records.

Customers, accounts, cards, transactions, payments, recipients, contact
information and transfer/idempotency records all live in the bank database.
Original transactions and new transfers use one transactions table. Balances
are integer cents. One SQLite transaction checks funds, debits, inserts the
transaction and records the outcome. Same-key retries return the same outcome;
changed payloads are rejected.

All operations are mock operations: no real bank, transfer, email or external
settlement is involved. Registered fake recipients are dingsijie@gmail.com and
abc@example.com. CAD is the displayed default and the demo fee is zero.
Outgoing transfers appear as purchase transactions for existing query filters.

## Multi-turn transfers

    You: I want to transfer some money to dingsijie@gmail.com.
    Agent: Please provide the amount.
    You: 500 dollars.
    Agent: Confirm transfer of CAD 500.00 from CHK-001 to dingsijie@gmail.com...
    You: Actually, make it 300.
    Agent: [Revalidates and displays the updated confirmation.]
    You: Pause the transfer.
    Agent: [Saves the draft.]
    You: What is my balance?
    Agent: [Queries the REST API.]
    You: Resume the transfer.
    Agent: [Revalidates and asks for fresh confirmation.]
    You: Yes.
    Agent: [Submits and reports the transaction ID.]

One unfinished transfer is allowed per conversation. Details may arrive across
turns or change together. Changes invalidate confirmation. An unrelated question
also invalidates pending confirmation: a subsequent "yes" displays a new summary.
Confirming while modifying details only updates the draft. /reset retains and
pauses a draft; resume or cancel it explicitly.

Cancellation before submission moves no money. In-flight or unknown outcomes
are reconciled through the API, not falsely reported as cancelled. A missing
record is an unknown outcome: an earlier HTTP request might still be queued.
Explicit "resume transfer" retries the already-authorized request using the SAME
idempotency key. Status checks and cancellation do not submit anything. An unknown
transfer cannot be declared cancelled merely because no record is visible.

## Task persistence and restart

The workflow database has only a tasks table. Its adjacent .checkpoints database
contains LangGraph state. Credentials and bank tokens are never saved.
Agent and bank database paths must differ.

Each graph turn ends at a waiting state. The next user message applies a typed
event to the saved task. Task snapshots are authoritative for recovery. Submission
intent is saved before the API call. Bank-side idempotency, not checkpointing
alone, prevents duplicate debit.

Reuse the printed conversation ID:

    BANK_CONVERSATION_ID=<previous-id> python app.py

Sign in as the same customer, then resume/check the task. Restored drafts require
fresh confirmation; already-authorized in-flight submissions are reconciled.

### Migration from the earlier combined database

Stop the old agent and bank service. Earlier code stored tasks and banking data
together in ~/.bank_agent/transfers.sqlite3. Do not point either new component
at that file. Preserve its records with:

    python -m banking_service.migrate ~/.bank_agent/transfers.sqlite3 \
      --bank-db ~/.bank_agent/bank.sqlite3 \
      --workflow-db ~/.bank_agent/workflows.sqlite3

Both destinations must be new files. The source remains untouched. Migration
copies balances, transfer outcomes and tasks without debiting again. Old graph
checkpoints are unnecessary because task snapshots are authoritative. If you
already started the new demo, choose new destination paths and configure BANK_DB
and BANK_WORKFLOW_DB accordingly.

## Files and tests

- app.py: chat, authenticated gateway, routing, task coordination.
- gateway.py: HTTP client only.
- banking_service/server.py: standalone REST service and session handling.
- banking_service/repository.py: SQL reads, authorization, atomic transfers.
- banking_service/seed.py: one-time fixtures.
- banking_service/migrate.py: offline legacy migration.
- bank_contracts.py: shared Pydantic REST models.
- workflows/: graph, parser, task models and task-only persistence.
- pi_agent.py / pi_banking_extension.ts: restricted read-only tool bridge.
- common_intents.py: fast-path responses using the REST gateway.

Tests start isolated real loopback HTTP servers and temporary databases. Models
are mocked; no manually running banking server is needed.

    python -m unittest test_bank_api test_transfer_workflow test_gateway test_authentication test_fast_path -v
    python -m unittest discover -v

The REST CI job runs the focused suite. Some legacy router/Pi tests still rely
on a model or expectations absent on main; see TRANSFER_VALIDATION.md. Live model
accuracy and latency are separate from API testing. SQLite/file locks target a
single macOS/Linux host, not a distributed production banking service.
