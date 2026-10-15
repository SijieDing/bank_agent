"""Public fake fixtures used only to initialize the bank database once."""
TRANSACTIONS = [
    {"description": "Costco", "amount": 128.32, "date": "2026-10-07", "type": "purchase"},
    {"description": "Rogers", "amount": 82.10, "date": "2026-10-06", "type": "purchase"},
    {"description": "Amazon", "amount": 49.99, "date": "2026-10-05", "type": "purchase"},
    {"description": "Savings transfer", "amount": 300.00, "date": "2026-10-01", "type": "saving"},
    {"description": "Grocery", "amount": 92.50, "date": "2026-09-15", "type": "purchase"},
    {"description": "Savings transfer", "amount": 200.00, "date": "2026-09-02", "type": "saving"},
    {"description": "Transit", "amount": 45.00, "date": "2026-08-20", "type": "purchase"},
    {"description": "Pharmacy", "amount": 23.75, "date": "2026-07-09", "type": "purchase"},
    {"description": "Savings transfer", "amount": 150.00, "date": "2026-07-08", "type": "saving"},
    {"description": "Coffee", "amount": 6.25, "date": "2026-06-30", "type": "purchase"},
]

ACCOUNTS = {
    "CHK-001": {
        "available_balance": 5320.45,
        "current_balance": 5480.20,
        "transactions": TRANSACTIONS,
        "payments": [{"type": "Mortgage", "amount": 2100.00, "date": "2026-10-09"}],
    },
    "CHK-002": {
        "available_balance": 2486.10,
        "current_balance": 2536.10,
        "transactions": [
            {"description": "Metro", "amount": 74.20, "date": "2026-10-08", "type": "purchase"},
            {"description": "Savings transfer", "amount": 450.00, "date": "2026-10-02", "type": "saving"},
            {"description": "Bell", "amount": 65.00, "date": "2026-09-26", "type": "purchase"},
            {"description": "Savings transfer", "amount": 100.00, "date": "2026-08-04", "type": "saving"},
        ],
        "payments": [{"type": "Rent", "amount": 1450.00, "date": "2026-10-15"}],
    },
    "CHK-003": {
        "available_balance": 9175.80,
        "current_balance": 9420.30,
        "transactions": [
            {"description": "Home Depot", "amount": 215.40, "date": "2026-10-06", "type": "purchase"},
            {"description": "Savings transfer", "amount": 800.00, "date": "2026-10-03", "type": "saving"},
            {"description": "Pet store", "amount": 48.50, "date": "2026-09-21", "type": "purchase"},
            {"description": "Savings transfer", "amount": 600.00, "date": "2026-07-10", "type": "saving"},
        ],
        "payments": [{"type": "Car loan", "amount": 425.00, "date": "2026-10-20"}],
    },
}

CARDS = {
    "VISA-001": {"current_balance": 1842.31, "statement_balance": 1661.20, "minimum_payment": 85.00, "payment_due_date": "2026-10-18"},
    "VISA-002": {"current_balance": 620.75, "statement_balance": 580.50, "minimum_payment": 30.00, "payment_due_date": "2026-10-22"},
    "VISA-003": {"current_balance": 3210.90, "statement_balance": 2980.15, "minimum_payment": 150.00, "payment_due_date": "2026-10-25"},
}


CUSTOMERS = {
    "4111111111111111": ("C10001", "CHK-001", "VISA-001", "6d658e2139a309e54871ab98bcd51407150d4dbdecb80b2a7652b636701683e3"),
    "5555555555554444": ("C10002", "CHK-002", "VISA-002", "c41f25d2fe19ba2c8725d677053e689735f077a4158fe9d73535f9d0dda3588f"),
    "4000000000000002": ("C10003", "CHK-003", "VISA-003", "d0a44434d2c7ffd062cb18790e0e34860ab75abdfffa2f3e18814fd21da2831c"),
}
RECIPIENTS = {"dingsijie@gmail.com": "external-demo-001", "abc@example.com": "external-demo-002"}
CONTACT = {"phone": "+1-800-555-0142", "address": "123 Bank Street, Toronto, ON M5V 1J1", "hours": "Monday to Friday, 9:00 AM to 5:00 PM (Eastern)"}
