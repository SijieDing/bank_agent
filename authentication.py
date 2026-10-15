"""Credential isolation at the chat boundary; verification belongs to the bank API."""
import re
import unicodedata


def authenticate_card(card_number, pin):
    """Compatibility helper; BankingAgent uses its own gateway's authenticated session."""
    from gateway import BankingToolGateway
    gateway = BankingToolGateway()
    try:
        return gateway.authenticate(card_number, pin)
    finally:
        gateway.close()


def contains_credentials(text: str) -> bool:
    """Reject card numbers and PINs before routing, history, or an LLM call."""
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
    if re.fullmatch(r"\s*[0-9]{4,6}\s*", text):
        return True
    sign_in = r"\b(?:sign[ -]?in|log[ -]?in|authenticate)\b"
    return bool(
        re.search(sign_in + r"[^0-9\n]{0,40}[0-9]{4,6}\b", text, re.IGNORECASE)
        or re.search(r"\b[0-9]{4,6}\b[^0-9\n]{0,25}" + sign_in, text, re.IGNORECASE)
    )
