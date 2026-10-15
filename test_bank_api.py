"""HTTP integration checks: no model, no direct agent database access."""
import ast
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

from bank_contracts import TransferDraft
from banking_service.repository import BankRepository
from pi_agent import PiBankingAgent
from test_support import BankServiceFixture
from workflows.store import WorkflowStore


class BankApiTests(unittest.TestCase):
    def setUp(self):
        self.service = BankServiceFixture()
        self.addCleanup(self.service.close)
        self.gateway = self.service.gateway()
        self.addCleanup(self.gateway.close)

    def draft(self, amount="500", account="CHK-001"):
        return TransferDraft(source_account_id=account, amount=amount,
                             recipient_email="dingsijie@gmail.com", currency="CAD")

    def test_service_authorizes_even_if_client_owner_is_forged(self):
        self.gateway._context["customer_id"] = "C10002"
        with self.assertRaises(ValueError):
            self.gateway.get_balance("CHK-002")
        with self.assertRaises(ValueError):
            self.gateway.validate_transfer("C10002", self.draft(account="CHK-002"))
        with self.assertRaises(ValueError):
            self.gateway._request("POST", "/transfers", {
                **self.draft().model_dump(mode="json"), "owner": "C10002"}, key="forged")

    def test_concurrent_debits_and_status_are_atomic(self):
        transfer = self.gateway.validate_transfer("C10001", self.draft("4000"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda key: self.gateway.submit_transfer("C10001", key, transfer), ["one", "two"]))
        self.assertEqual(sorted(result["status"] for result in outcomes), ["FAILED", "SUCCEEDED"])
        self.assertEqual(self.gateway.get_balance("CHK-001")["available_balance"], 1320.45)
        for key, result in zip(["one", "two"], outcomes):
            self.assertEqual(self.gateway.get_transfer_status("C10001", key), result)

    def test_all_records_persist_and_seed_does_not_overwrite(self):
        transfer = self.gateway.validate_transfer("C10001", self.draft())
        result = self.gateway.submit_transfer("C10001", "persist", transfer)
        reopened = BankRepository(self.service.repository.path)
        self.assertEqual(reopened.get_balance("C10001", "CHK-001")["available_balance"], 4820.45)
        self.assertEqual(reopened.get_transfer_status("C10001", "persist"), result)
        transactions = reopened.get_transactions("C10001", "CHK-001")["transactions"]
        self.assertEqual(len(transactions), 11)
        with reopened.connect() as db:
            db.execute("UPDATE contact SET data=? WHERE id=1", (json.dumps({"phone": "database value"}),))
            db.execute("UPDATE recipients SET id='new-id' WHERE email='abc@example.com'")
        self.assertEqual(self.gateway.get_contact_information()["phone"], "database value")
        self.assertEqual(reopened.get_upcoming_payments("C10001", "CHK-001")["payments"][0]["type"], "Mortgage")
        self.assertEqual(reopened.get_credit_card_summary("C10001", "VISA-001")["current_balance"], 1842.31)

    def test_tokens_revoked_and_not_saved_in_agent_context(self):
        context = self.gateway._context.copy()
        self.assertNotIn("token", context)
        old_token = self.gateway._token
        self.gateway.close()
        self.gateway._token = old_token
        with self.assertRaises(PermissionError):
            self.gateway.get_balance("CHK-001")

    def test_separate_database_boundaries(self):
        store = WorkflowStore(self.service.directory / "workflows.sqlite3")
        with store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"tasks"})
        with self.assertRaises(ValueError):
            WorkflowStore(self.service.repository.path)
        with self.assertRaises(ValueError):
            BankRepository(store.path)
        source = Path(__file__).with_name("gateway.py").read_text()
        imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any("sqlite" in ast.unparse(node) or "banking_service" in ast.unparse(node) for node in imports))

    def test_pi_bridge_reads_bank_api_without_exposing_bank_token(self):
        opener = build_opener(ProxyHandler({}))
        def fake_pi(message, policy, tools, env):
            self.assertNotIn(self.gateway._token, message + policy + json.dumps(env))
            request = Request(env["BANK_AGENT_URL"],
                              data=json.dumps({"tool": "get_balance", "account_id": "CHK-002"}).encode(),
                              headers={"Authorization": "Bearer " + env["BANK_AGENT_TOKEN"]})
            with opener.open(request) as response:
                data = json.load(response)
            self.assertEqual(data["available_balance"], 5320.45)
            self.assertNotIn("account_id", data)
            return "Your available balance is CAD 5,320.45."
        with patch.object(PiBankingAgent, "_run_pi", side_effect=fake_pi):
            reply = PiBankingAgent(self.gateway).handle("What is my balance?", self.gateway._context,
                                                       ("get_balance",))
        self.assertIn("5,320.45", reply.answer)

    def test_bad_and_cross_customer_requests_fail_at_http_boundary(self):
        opener = build_opener(ProxyHandler({}))
        for path, status in [("/accounts/CHK-001/balance", 401), ("/transfers/by-key/key", 401)]:
            with self.assertRaises(HTTPError) as error:
                opener.open(self.service.url + path)
            self.assertEqual(error.exception.code, status)
        request = Request(self.service.url + "/accounts/CHK-002/balance",
                          headers={"Authorization": "Bearer " + self.gateway._token})
        with self.assertRaises(HTTPError) as error:
            opener.open(request)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(ValueError):
            self.gateway._request("POST", "/transfers", self.draft().model_dump(mode="json"))
