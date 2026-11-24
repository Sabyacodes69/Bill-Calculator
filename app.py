"""FastAPI authentication, bill, and PhonePe checkout endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.database import connection, initialize_database
from backend.phonepe import PhonePeClient, PhonePeError
from backend.security import create_session, hash_password, token_digest, verify_password


phonepe = PhonePeClient()
bearer_scheme = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Electricity Bill Payment API",
    version="1.0.0",
    description="Role-protected bill access and PhonePe UPI checkout.",
    lifespan=lifespan,
)


class LoginRequest(BaseModel):
    role: Literal["admin", "consumer"]
    username: str = Field(min_length=1, max_length=100)
    password: str


class RegisterRequest(BaseModel):
    consumer_number: str = Field(min_length=1, max_length=100)
    registration_code: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)


class ConsumerCreateRequest(BaseModel):
    consumer_number: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    discom: Literal["TPCODL", "TPNODL", "TPWODL", "TPSODL"]
    category: Literal[
        "Domestic General",
        "Kutir Jyoti",
        "Single-phase domestic",
        "Three-phase domestic",
    ]
    sanctioned_load: float = Field(gt=0, le=1000)
    meter_type: Literal[
        "Single-phase meter", "Three-phase meter", "Smart meter"
    ]
    meter_multiplier: float = Field(gt=0, le=1000)


class PaymentOrderRequest(BaseModel):
    bill_id: int = Field(gt=0)


@dataclass(frozen=True)
class Principal:
    role: str
    principal_id: str


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    digest = token_digest(credentials.credentials)
    with connection() as db:
        session = db.execute(
            """
            SELECT role, principal_id FROM api_sessions
            WHERE token_hash=? AND expires_at>?
            """,
            (digest, time.time()),
        ).fetchone()
    if session is None:
        raise _unauthorized()
    return Principal(role=session["role"], principal_id=session["principal_id"])


def require_admin(
    principal: Principal = Depends(current_principal),
) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required.")
    return principal


def require_consumer(
    principal: Principal = Depends(current_principal),
) -> Principal:
    if principal.role != "consumer":
        raise HTTPException(status_code=403, detail="Consumer role required.")
    return principal


def _paise(amount: Any) -> int:
    try:
        rupees = Decimal(str(amount)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, ValueError) as error:
        raise HTTPException(status_code=409, detail="Bill amount is invalid.") from error
    return int(rupees * 100)


def _bill_balance(
    db: Any, bill_id: int, consumer_number: str
) -> tuple[Any, int, int] | None:
    bill = db.execute(
        """
        SELECT id, consumer_number, billing_date, due_date, total
        FROM bills
        WHERE id=? AND consumer_number=?
        """,
        (bill_id, consumer_number),
    ).fetchone()
    if bill is None:
        return None
    paid = db.execute(
        """
        SELECT COALESCE(SUM(amount_paise), 0) AS paid_amount
        FROM payment_orders
        WHERE bill_id=? AND consumer_number=? AND state='COMPLETED'
        """,
        (bill_id, consumer_number),
    ).fetchone()["paid_amount"]
    total_paise = _paise(bill["total"])
    return bill, total_paise, max(0, total_paise - int(paid))


def _payment_response(row: Any) -> dict[str, Any]:
    return {
        "merchant_order_id": row["merchant_order_id"],
        "bill_id": row["bill_id"],
        "amount_paise": row["amount_paise"],
        "currency": "INR",
        "state": row["state"],
        "redirect_url": row["redirect_url"],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    with connection() as db:
        if payload.role == "admin":
            account = db.execute(
                "SELECT password_hash FROM admin_accounts WHERE username=?",
                (payload.username.strip(),),
            ).fetchone()
        else:
            account = db.execute(
                """
                SELECT password_hash FROM consumer_accounts
                WHERE consumer_number=?
                """,
                (payload.username.strip(),),
            ).fetchone()

        if account is None or not verify_password(
            payload.password, account["password_hash"]
        ):
            raise _unauthorized()
        token, expires_at = create_session(
            db, payload.role, payload.username.strip()
        )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at,
        "role": payload.role,
    }


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register_consumer(payload: RegisterRequest) -> dict[str, str]:
    consumer_number = payload.consumer_number.strip()
    with connection() as db:
        consumer = db.execute(
            "SELECT 1 FROM consumers WHERE consumer_number=?",
            (consumer_number,),
        ).fetchone()
        if consumer is None:
            raise HTTPException(
                status_code=404,
                detail="Ask the administrator to create this consumer ID first.",
            )
        account = db.execute(
            "SELECT 1 FROM consumer_accounts WHERE consumer_number=?",
            (consumer_number,),
        ).fetchone()
        if account is not None:
            raise HTTPException(
                status_code=409, detail="This consumer ID already has a login."
            )
        enrollment = db.execute(
            """
            SELECT code_hash, expires_at FROM consumer_registration_codes
            WHERE consumer_number=?
            """,
            (consumer_number,),
        ).fetchone()
        registration_code_hash = hashlib.sha256(
            payload.registration_code.strip().encode("utf-8")
        ).hexdigest()
        if (
            enrollment is None
            or enrollment["expires_at"] <= time.time()
            or not hmac.compare_digest(
                registration_code_hash, enrollment["code_hash"]
            )
        ):
            raise HTTPException(
                status_code=401,
                detail="The one-time registration code is invalid or expired.",
            )
        db.execute(
            """
            INSERT INTO consumer_accounts (consumer_number, password_hash)
            VALUES (?, ?)
            """,
            (consumer_number, hash_password(payload.password)),
        )
        db.execute(
            "DELETE FROM consumer_registration_codes WHERE consumer_number=?",
            (consumer_number,),
        )
    return {"consumer_number": consumer_number, "status": "registered"}


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    _principal: Principal = Depends(current_principal),
) -> None:
    if credentials is None:
        raise _unauthorized()
    with connection() as db:
        db.execute(
            "DELETE FROM api_sessions WHERE token_hash=?",
            (token_digest(credentials.credentials),),
        )


@app.get("/api/auth/me")
def who_am_i(
    principal: Principal = Depends(current_principal),
) -> dict[str, str]:
    return {"role": principal.role, "principal_id": principal.principal_id}


@app.post("/api/admin/consumers", status_code=status.HTTP_201_CREATED)
def create_consumer(
    payload: ConsumerCreateRequest,
    _admin: Principal = Depends(require_admin),
) -> dict[str, str]:
    registration_code = secrets.token_urlsafe(9)
    with connection() as db:
        exists = db.execute(
            "SELECT 1 FROM consumers WHERE consumer_number=?",
            (payload.consumer_number.strip(),),
        ).fetchone()
        if exists is not None:
            raise HTTPException(status_code=409, detail="Consumer ID already exists.")
        db.execute(
            """
            INSERT INTO consumers (
                consumer_number, name, discom, category, sanctioned_load,
                meter_type, meter_multiplier
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.consumer_number.strip(),
                payload.name.strip(),
                payload.discom,
                payload.category,
                payload.sanctioned_load,
                payload.meter_type,
                payload.meter_multiplier,
            ),
        )
        db.execute(
            """
            INSERT INTO consumer_registration_codes (
                consumer_number, code_hash, expires_at
            ) VALUES (?, ?, ?)
            """,
            (
                payload.consumer_number.strip(),
                hashlib.sha256(registration_code.encode("utf-8")).hexdigest(),
                time.time() + 7 * 24 * 60 * 60,
            ),
        )
    return {
        "consumer_number": payload.consumer_number.strip(),
        "registration_code": registration_code,
        "status": "created",
    }


@app.get("/api/payments/due")
def list_consumer_dues(
    consumer: Principal = Depends(require_consumer),
) -> dict[str, Any]:
    with connection() as db:
        bills = db.execute(
            """
            SELECT id FROM bills
            WHERE consumer_number=?
            ORDER BY due_date, id
            """,
            (consumer.principal_id,),
        ).fetchall()
        due_items = []
        for item in bills:
            balance = _bill_balance(db, item["id"], consumer.principal_id)
            if balance is None:
                continue
            bill, total_paise, due_paise = balance
            if due_paise > 0:
                due_items.append(
                    {
                        "bill_id": bill["id"],
                        "billing_date": bill["billing_date"],
                        "due_date": bill["due_date"],
                        "bill_amount_paise": total_paise,
                        "due_amount_paise": due_paise,
                        "currency": "INR",
                    }
                )
    return {"consumer_number": consumer.principal_id, "bills": due_items}


@app.post("/api/payments/orders", status_code=status.HTTP_201_CREATED)
def create_payment_order(
    payload: PaymentOrderRequest,
    consumer: Principal = Depends(require_consumer),
) -> dict[str, Any]:
    now = time.time()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        balance = _bill_balance(db, payload.bill_id, consumer.principal_id)
        if balance is None:
            raise HTTPException(status_code=404, detail="Bill not found.")
        _bill, _total_paise, due_paise = balance
        if due_paise <= 0:
            raise HTTPException(status_code=409, detail="This bill has no active due.")

        pending = db.execute(
            """
            SELECT * FROM payment_orders
            WHERE bill_id=? AND consumer_number=?
              AND state IN ('CREATING', 'PENDING')
            ORDER BY created_at DESC LIMIT 1
            """,
            (payload.bill_id, consumer.principal_id),
        ).fetchone()
        if pending is not None:
            if pending["state"] == "PENDING" and pending["redirect_url"]:
                return _payment_response(pending)
            if now - pending["created_at"] < 60:
                raise HTTPException(
                    status_code=409,
                    detail="A payment order is already being created for this bill.",
                )
            db.execute(
                """
                UPDATE payment_orders
                SET state='FAILED', provider_state='CREATE_TIMEOUT', updated_at=?
                WHERE merchant_order_id=? AND state='CREATING'
                """,
                (now, pending["merchant_order_id"]),
            )
        if not phonepe.is_configured():
            raise HTTPException(
                status_code=503,
                detail="PhonePe backend credentials and redirect URL are not configured.",
            )

        merchant_order_id = f"bill-{payload.bill_id}-{secrets.token_hex(12)}"
        db.execute(
            """
            INSERT INTO payment_orders (
                merchant_order_id, bill_id, consumer_number, amount_paise,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'CREATING', ?, ?)
            """,
            (
                merchant_order_id,
                payload.bill_id,
                consumer.principal_id,
                due_paise,
                now,
                now,
            ),
        )

    try:
        provider_order = phonepe.create_order(merchant_order_id, due_paise)
    except PhonePeError as error:
        with connection() as db:
            db.execute(
                """
                UPDATE payment_orders
                SET state='FAILED', provider_state='CREATE_ERROR', updated_at=?
                WHERE merchant_order_id=? AND state='CREATING'
                """,
                (time.time(), merchant_order_id),
            )
        raise HTTPException(
            status_code=502, detail="PhonePe checkout could not be created."
        ) from error

    with connection() as db:
        db.execute(
            """
            UPDATE payment_orders
            SET state='PENDING', phonepe_order_id=?, redirect_url=?,
                provider_state='PENDING', updated_at=?
            WHERE merchant_order_id=? AND state='CREATING'
            """,
            (
                provider_order["orderId"],
                provider_order["redirectUrl"],
                time.time(),
                merchant_order_id,
            ),
        )
        order = db.execute(
            "SELECT * FROM payment_orders WHERE merchant_order_id=?",
            (merchant_order_id,),
        ).fetchone()
    return _payment_response(order)


@app.get("/api/payments/orders/{merchant_order_id}")
def get_payment_status(
    merchant_order_id: str,
    consumer: Principal = Depends(require_consumer),
) -> dict[str, Any]:
    with connection() as db:
        order = db.execute(
            """
            SELECT * FROM payment_orders
            WHERE merchant_order_id=? AND consumer_number=?
            """,
            (merchant_order_id, consumer.principal_id),
        ).fetchone()
    if order is None:
        raise HTTPException(status_code=404, detail="Payment order not found.")
    if order["state"] in ("COMPLETED", "FAILED"):
        return _payment_response(order)

    try:
        provider_status = phonepe.get_order_status(merchant_order_id)
    except PhonePeError as error:
        raise HTTPException(
            status_code=502, detail="PhonePe payment status is temporarily unavailable."
        ) from error

    if (
        provider_status.get("orderId") != order["phonepe_order_id"]
        or (
            provider_status.get("merchantOrderId") is not None
            and provider_status["merchantOrderId"] != merchant_order_id
        )
    ):
        raise HTTPException(
            status_code=502, detail="PhonePe returned a different payment order."
        )
    provider_state = provider_status.get("state")
    if provider_state not in ("PENDING", "COMPLETED", "FAILED"):
        raise HTTPException(
            status_code=502, detail="PhonePe returned an unknown payment status."
        )
    provider_amount = provider_status.get("amount")
    try:
        provider_amount_paise = (
            int(provider_amount)
            if isinstance(provider_amount, (int, str)) and not isinstance(provider_amount, bool)
            else None
        )
    except ValueError:
        provider_amount_paise = None
    if provider_amount_paise != order["amount_paise"]:
        raise HTTPException(
            status_code=502, detail="PhonePe payment amount did not match the order."
        )

    with connection() as db:
        db.execute(
            """
            UPDATE payment_orders
            SET state=CASE WHEN state='COMPLETED' THEN state ELSE ? END,
                provider_state=?, updated_at=?
            WHERE merchant_order_id=? AND consumer_number=?
            """,
            (
                provider_state,
                provider_state,
                time.time(),
                merchant_order_id,
                consumer.principal_id,
            ),
        )
        updated = db.execute(
            """
            SELECT * FROM payment_orders
            WHERE merchant_order_id=? AND consumer_number=?
            """,
            (merchant_order_id, consumer.principal_id),
        ).fetchone()
    return _payment_response(updated)


def _webhook_credentials_match(authorization: str | None) -> bool:
    username = os.environ.get("PHONEPE_WEBHOOK_USERNAME")
    password = os.environ.get("PHONEPE_WEBHOOK_PASSWORD")
    if not username or not password or not authorization:
        return False
    expected = hashlib.sha256(f"{username}:{password}".encode("utf-8")).hexdigest()
    supplied = authorization.strip()
    for prefix in ("SHA256:", "SHA256 "):
        if supplied.upper().startswith(prefix):
            supplied = supplied[len(prefix):]
            break
    return hmac.compare_digest(supplied.lower(), expected)


@app.post("/api/payments/phonepe/webhook")
async def phonepe_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, bool]:
    if not _webhook_credentials_match(authorization):
        raise HTTPException(status_code=401, detail="Invalid webhook authorization.")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Webhook payload is too large.")
    try:
        event_data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON.") from error
    if not isinstance(event_data, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")

    event = event_data.get("event")
    payload = event_data.get("payload")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload is missing.")
    merchant_order_id = payload.get("merchantOrderId")
    phonepe_order_id = payload.get("orderId")
    provider_state = payload.get("state")
    if event == "checkout.order.completed":
        expected_state = "COMPLETED"
    elif event == "checkout.order.failed":
        expected_state = "FAILED"
    else:
        return {"received": True}
    if (
        provider_state != expected_state
        or not isinstance(merchant_order_id, str)
        or not isinstance(phonepe_order_id, str)
    ):
        raise HTTPException(status_code=400, detail="Webhook event state is invalid.")

    amount = payload.get("amount")
    with connection() as db:
        order = db.execute(
            "SELECT * FROM payment_orders WHERE merchant_order_id=?",
            (merchant_order_id,),
        ).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Payment order not found.")
        if phonepe_order_id != order["phonepe_order_id"]:
            raise HTTPException(
                status_code=400, detail="Webhook payment order did not match."
            )
        if not isinstance(amount, int) or amount != order["amount_paise"]:
            raise HTTPException(status_code=400, detail="Webhook amount did not match.")
        if order["state"] != "COMPLETED":
            db.execute(
                """
                UPDATE payment_orders
                SET state=?, provider_state=?, updated_at=?
                WHERE merchant_order_id=?
                """,
                (expected_state, provider_state, time.time(), merchant_order_id),
            )
    return {"received": True}
