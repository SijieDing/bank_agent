"""Local authentication for public demo cards; credentials never enter the agent."""

import hashlib
import hmac
import re
import unicodedata


# These are public demo accounts, not production authentication records.
_CARDS = {
    "4111111111111111": ("C10001", "CHK-001", "VISA-001", "6d658e2139a309e54871ab98bcd51407150d4dbdecb80b2a7652b636701683e3"),
    "5555555555554444": ("C10002", "CHK-002", "VISA-002", "c41f25d2fe19ba2c8725d677053e689735f077a4158fe9d73535f9d0dda3588f"),
    "4000000000000002": ("C10003", "CHK-003", "VISA-003", "d0a44434d2c7ffd062cb18790e0e34860ab75abdfffa2f3e18814fd21da2831c"),
}


def authenticate_card(card_number: str, pin: str) -> dict[str, str] | None:
    """Verify locally and return only the customer's noncredential identifiers."""
    if not isinstance(card_number, str) or not isinstance(pin, str):
        return None
    if len(card_number) > 64 or not re.fullmatch(r"[0-9 -]+", card_number):
        return None
    if not re.fullmatch(r"[0-9]{4,6}", pin):
        return None
    card = _CARDS.get(card_number.replace(" ", "").replace("-", ""))
    if card is None:
        return None
    customer_id, account_id, card_id, expected = card
    verifier = hashlib.pbkdf2_hmac(
        "sha256", pin.encode(), ("bank-demo:" + customer_id).encode(), 100_000,
    ).hex()
    if not hmac.compare_digest(verifier, expected):
        return None
    return {
        "customer_id": customer_id,
        "default_account_id": account_id,
        "default_card_id": card_id,
    }


def contains_credentials(text: str) -> bool:
    """Reject likely credentials before chat history, routing, or an LLM call.

    This conservative guard recognizes ordinary numeric credentials, not arbitrary
    encodings or numbers written as words. It can reject unrelated long identifiers.
    Credentials belong in the separate local authentication input, never in chat.
    """
    if not isinstance(text, str):
        return True
    text = unicodedata.normalize("NFKC", text)
    text = "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in text)
    if re.search(r"(?<![0-9])[0-9](?:[ \t-]*[0-9]){11,}(?![0-9])", text):
        return True
    label = r"\b(?:pin(?:\s+number)?|passcode|card\s+(?:number|no\.?))\b"
    if re.search(label + r"[^0-9\n]{0,40}[0-9]", text, re.IGNORECASE):
        return True
    if re.search(r"\b[0-9]{4,6}\b[^0-9\n]{0,25}" + label, text, re.IGNORECASE):
        return True

    # Ordinary dates and amounts can contain four to six digits without being PINs.
    remainder = re.sub(r"\b[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}\b", "", text)
    remainder = re.sub(r"\b[0-9]{1,2}[-/][0-9]{1,2}[-/][0-9]{4}\b", "", remainder)
    month = (
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    )
    remainder = re.sub(
        r"\b" + month + r"\s+[0-9]{1,2},?\s+(?:19|20)[0-9]{2}\b",
        "", remainder, flags=re.IGNORECASE,
    )
    amount = r"[0-9][0-9,]*(?:\.[0-9]{1,2})?"
    remainder = re.sub(
        r"(?:[$€£]\s*|\b(?:CAD|USD|EUR|GBP)\s+)" + amount
        + "|" + amount + r"\s*(?:CAD|USD|EUR|GBP|dollars?)\b",
        "", remainder, flags=re.IGNORECASE,
    )
    remainder = re.sub(r"\b[0-9][0-9,]*\.[0-9]{1,2}\b", "", remainder)
    remainder = re.sub(
        r"\b(?:spent?|spend(?:ing)?|pay(?:ment)?|paid|transfer|deposit|withdraw"
        r"|withdrawal|balance|budget|cost)\s+(?:of\s+|is\s+|was\s+|over\s+)?"
        + amount, "", remainder, flags=re.IGNORECASE,
    )
    if remainder.strip().isdigit():
        return bool(re.fullmatch(r"[0-9]{4,6}", remainder.strip()))
    year = r"(?:19|20)[0-9]{2}"
    remainder = re.sub(
        r"\b(?:in|during|year|since|before|after|from|through|until)\s+" + year + r"\b"
        + r"|\b" + year + r"\s+(?:transactions|spending|purchases|payments|savings|statement|history)\b"
        + r"|\b" + month + r"\s+" + year + r"\b",
        "", remainder, flags=re.IGNORECASE,
    )
    return bool(re.search(r"(?<![0-9.,])[0-9]{4,6}(?![0-9.,])", remainder))
