"""Password verification and opaque API-session helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import sqlite3


PASSWORD_HASH_ITERATIONS = 310_000
SESSION_LIFETIME_SECONDS = 8 * 60 * 60


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return (
        f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}"
        f"${salt.hex()}${password_hash.hex()}"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, rounds_text, salt_text, expected_text = stored_hash.split("$")
        rounds = int(rounds_text)
        if algorithm != "pbkdf2_sha256" or not 1 <= rounds <= 1_000_000:
            return False
        salt = bytes.fromhex(salt_text)
        expected = bytes.fromhex(expected_text)
    except (TypeError, ValueError):
        return False
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, rounds
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(
    db: sqlite3.Connection, role: str, principal_id: str
) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + SESSION_LIFETIME_SECONDS
    db.execute(
        """
        INSERT INTO api_sessions (token_hash, role, principal_id, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (token_digest(token), role, principal_id, expires_at),
    )
    db.execute("DELETE FROM api_sessions WHERE expires_at <= ?", (time.time(),))
    return token, expires_at
