"""SQLite persistence shared by the desktop calculator and payment API."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parents[1] / "bill_calculator.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS consumers (
    consumer_number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    discom TEXT NOT NULL,
    category TEXT NOT NULL,
    sanctioned_load REAL NOT NULL,
    meter_type TEXT NOT NULL,
    meter_multiplier REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumer_number TEXT NOT NULL REFERENCES consumers(consumer_number),
    billing_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    payment_date TEXT,
    previous_reading REAL NOT NULL,
    current_reading REAL NOT NULL,
    units REAL NOT NULL,
    energy_charge REAL NOT NULL,
    fixed_charge REAL NOT NULL,
    meter_rent REAL NOT NULL,
    duty_percent REAL NOT NULL,
    electricity_duty REAL NOT NULL,
    other_charges REAL NOT NULL,
    rebate_percent REAL NOT NULL,
    prompt_payment_rebate REAL NOT NULL,
    surcharge_percent REAL NOT NULL,
    delayed_payment_surcharge REAL NOT NULL,
    total REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admin_accounts (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_accounts (
    consumer_number TEXT PRIMARY KEY
        REFERENCES consumers(consumer_number) ON DELETE CASCADE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_registration_codes (
    consumer_number TEXT PRIMARY KEY
        REFERENCES consumers(consumer_number) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_sessions (
    token_hash TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK (role IN ('admin', 'consumer')),
    principal_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_api_sessions_expiry
    ON api_sessions(expires_at);
CREATE TABLE IF NOT EXISTS payment_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_order_id TEXT NOT NULL UNIQUE,
    bill_id INTEGER NOT NULL REFERENCES bills(id),
    consumer_number TEXT NOT NULL REFERENCES consumers(consumer_number),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    state TEXT NOT NULL CHECK (
        state IN ('CREATING', 'PENDING', 'COMPLETED', 'FAILED')
    ),
    phonepe_order_id TEXT,
    redirect_url TEXT,
    provider_state TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    updated_at REAL NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_payment_orders_bill_state
    ON payment_orders(bill_id, state);
CREATE INDEX IF NOT EXISTS idx_payment_orders_consumer
    ON payment_orders(consumer_number, created_at);
"""


def database_path() -> Path:
    configured_path = os.environ.get("BILL_CALCULATOR_DB_PATH")
    return Path(configured_path).expanduser() if configured_path else DEFAULT_DATABASE_PATH


@contextmanager
def connection() -> Generator[sqlite3.Connection, None, None]:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize_database() -> None:
    with connection() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
