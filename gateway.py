"""Banking REST client. No database access, seed fixtures, or model-facing token."""
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from bank_contracts import ValidatedTransfer


class BankingToolGateway:
    def __init__(self, base_url=None, timeout=15):
        self.base_url = (base_url or os.getenv("BANK_API_URL", "http://127.0.0.1:8765")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("BANK_API_URL must be an HTTP(S) service URL without embedded credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Non-loopback banking services require HTTPS")
        self.timeout = timeout
        self._token = None
        self._context = None
        self._opener = build_opener(ProxyHandler({})) if parsed.hostname in {"127.0.0.1", "localhost", "::1"} else build_opener()

    def _request(self, method, path, data=None, *, params=None, key=None):
        url = self.base_url + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if key is not None:
            headers["Idempotency-Key"] = key
        request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                          headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            # Never expose request bodies or the Authorization header in errors.
            try:
                message = json.load(exc).get("error", "Bank API rejected request")
            except (ValueError, AttributeError):
                message = "Bank API rejected request"
            if exc.code == 401:
                raise PermissionError("Bank sign-in is required or has expired") from None
            if exc.code in {400, 403, 404, 409, 422}:
                raise ValueError(message) from None
            raise RuntimeError("Bank API outcome unknown; check transfer status") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError("Bank API unavailable or returned an invalid response") from None

    def authenticate(self, card_number, pin):
        if self._context is not None:
            raise ValueError("This gateway is already signed in")
        try:
            result = self._request("POST", "/auth", {"card_number": card_number, "pin": pin})
        except PermissionError:
            return None
        self._token = result["token"]
        self._context = dict(result["context"])
        return dict(self._context)

    def close(self):
        try:
            if self._token:
                self._request("POST", "/logout", {})
        except (ValueError, RuntimeError, PermissionError):
            pass
        finally:
            self._token = None
            self._context = None

    def _owner(self, owner):
        if not self._context or self._context["customer_id"] != owner:
            raise ValueError("Authenticated customer does not match task owner")

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not value:
            raise ValueError("An account, card, or transaction identifier is required")
        return quote(value, safe="")

    def get_balance(self, account_id):
        return self._request("GET", f"/accounts/{self._id(account_id)}/balance")

    def get_transactions(self, account_id, start_date=None, end_date=None,
                         transaction_type="both", limit=None, minimum_amount=None,
                         maximum_amount=None, store_name=None):
        if limit is not None and type(limit) is not int:
            raise ValueError("limit must be an integer")
        return self._request("GET", f"/accounts/{self._id(account_id)}/transactions", params={
            "start_date": start_date, "end_date": end_date, "transaction_type": transaction_type,
            "limit": limit, "minimum_amount": minimum_amount, "maximum_amount": maximum_amount,
            "store_name": store_name})

    def get_recent_transactions(self, account_id, limit=5):
        return self.get_transactions(account_id, limit=limit)

    def search_transactions(self, account_id, merchant=None):
        return self.get_transactions(account_id, store_name=merchant or None)

    def get_credit_card_summary(self, card_id):
        return self._request("GET", f"/cards/{self._id(card_id)}/summary")

    def get_upcoming_payments(self, account_id):
        return self._request("GET", f"/accounts/{self._id(account_id)}/payments")

    def get_contact_information(self):
        return self._request("GET", "/contact")

    def list_transfer_accounts(self, owner):
        self._owner(owner)
        return self._request("GET", "/accounts")

    def validate_transfer(self, owner, draft):
        self._owner(owner)
        return ValidatedTransfer.model_validate(self._request(
            "POST", "/transfers/validate", draft.model_dump(mode="json")))

    def submit_transfer(self, owner, key, transfer):
        self._owner(owner)
        return self._request("POST", "/transfers",
                             transfer.model_dump(mode="json", exclude={"recipient_id"}), key=key)

    def get_transfer_status(self, owner, key):
        self._owner(owner)
        return self._request("GET", "/transfers/by-key/" + self._id(key))
