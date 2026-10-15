"""Real loopback REST service fixtures; no model or external service is needed."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from banking_service.repository import BankRepository
from banking_service.server import BankServer
from gateway import BankingToolGateway

CREDENTIALS = [
    ("4111111111111111", "1234"),
    ("5555555555554444", "2345"),
    ("4000000000000002", "3456"),
]


class BankServiceFixture:
    def __init__(self, directory=None):
        self.temp = tempfile.TemporaryDirectory() if directory is None else None
        self.directory = Path(directory or self.temp.name)
        self.repository = BankRepository(self.directory / "bank.sqlite3")
        self.server = BankServer(("127.0.0.1", 0), self.repository)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def gateway(self, customer=1):
        gateway = BankingToolGateway(self.url)
        if customer is not None:
            assert gateway.authenticate(*CREDENTIALS[customer - 1])
        return gateway

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.temp:
            self.temp.cleanup()


def module_service():
    service = BankServiceFixture()
    unittest.addModuleCleanup(service.close)
    env = patch.dict(os.environ, {"BANK_API_URL": service.url,
                                 "BANK_WORKFLOW_DB": str(service.directory / "workflows.sqlite3")})
    env.start()
    unittest.addModuleCleanup(env.stop)


def authenticated_gateway(customer=1):
    gateway = BankingToolGateway()
    assert gateway.authenticate(*CREDENTIALS[customer - 1])
    return gateway
