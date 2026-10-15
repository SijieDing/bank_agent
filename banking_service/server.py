"""Run separately: python -m banking_service.server --db ~/.bank_agent/bank.sqlite3."""
import argparse
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from bank_contracts import TransferDraft
from banking_service.repository import BankRepository


class BankServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, repository):
        self.repository = repository
        self.sessions = {}
        self.session_lock = threading.Lock()
        super().__init__(address, BankHandler)

    def issue_session(self, context):
        token = secrets.token_urlsafe(32)
        with self.session_lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}
            self.sessions[token] = (context, now + 3600)
        return token

    def context(self, authorization):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        with self.session_lock:
            entry = self.sessions.get(token)
            if not entry or entry[1] <= time.monotonic():
                self.sessions.pop(token, None)
                return None
            return dict(entry[0])


class BankHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log credentials, session tokens or request bodies.

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def handle_request(self, method):
        try:
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("Repeated query parameters are not supported")
            params = {key: values[0] for key, values in query.items()}
            data = None
            if method == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
            repo = self.server.repository
            if method == "GET" and path == "/contact":
                self.send_json(200, repo.get_contact_information())
                return
            if method == "POST" and path == "/auth":
                if set(data) != {"card_number", "pin"}:
                    self.send_json(400, {"error": "Provide card_number and pin"})
                    return
                context = repo.authenticate(data["card_number"], data["pin"])
                if context is None:
                    self.send_json(401, {"error": "Invalid credentials"})
                    return
                self.send_json(200, {"context": context, "token": self.server.issue_session(context)})
                return
            authorization = self.headers.get("Authorization", "")
            context = self.server.context(authorization)
            if context is None:
                self.send_json(401, {"error": "Sign in to the banking service"})
                return
            owner = context["customer_id"]
            if method == "POST" and path == "/logout":
                with self.server.session_lock:
                    self.server.sessions.pop(authorization.removeprefix("Bearer "), None)
                result = {"success": True}
            elif method == "GET" and path == "/accounts":
                result = repo.list_accounts(owner)
            elif method == "POST" and path in {"/transfers/validate", "/transfers"}:
                draft = TransferDraft.model_validate(data)
                if path.endswith("/validate"):
                    result = repo.validate_transfer(owner, draft).model_dump(mode="json")
                else:
                    result = repo.submit_transfer(owner, self.headers.get("Idempotency-Key"), draft)
            elif method == "GET" and path.startswith("/transfers/by-key/"):
                result = repo.get_transfer_status(owner, unquote(path[len("/transfers/by-key/"):]))
            else:
                parts = [unquote(part) for part in path.strip("/").split("/")]
                if method == "GET" and len(parts) == 3 and parts[0] == "accounts":
                    account, operation = parts[1:]
                    if operation == "balance":
                        result = repo.get_balance(owner, account)
                    elif operation == "payments":
                        result = repo.get_upcoming_payments(owner, account)
                    elif operation == "transactions":
                        allowed = {"start_date", "end_date", "transaction_type", "limit",
                                   "minimum_amount", "maximum_amount", "store_name"}
                        if set(params) - allowed:
                            raise ValueError("Unsupported transaction filter")
                        if "limit" in params:
                            params["limit"] = int(params["limit"])
                        result = repo.get_transactions(owner, account, **params)
                    else:
                        self.send_json(404, {"error": "Unknown endpoint"})
                        return
                elif method == "GET" and len(parts) == 3 and parts[0] == "cards" and parts[2] == "summary":
                    result = repo.get_credit_card_summary(owner, parts[1])
                else:
                    self.send_json(404, {"error": "Unknown endpoint"})
                    return
            self.send_json(200, result)
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
        except (ValueError, TypeError, KeyError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception:
            # A failed response must not imply a transfer failed: the client reconciles.
            self.send_json(500, {"error": "Bank service unavailable; check submission status"})


def main():
    parser = argparse.ArgumentParser(description="Local mock banking REST service")
    parser.add_argument("--db", default=os.getenv("BANK_DB", str(Path.home() / ".bank_agent" / "bank.sqlite3")))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # Intentionally loopback-only: use a production HTTP stack and TLS for deployment.
    with BankServer(("127.0.0.1", args.port), BankRepository(args.db)) as server:
        print(f"Mock banking API: http://127.0.0.1:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
