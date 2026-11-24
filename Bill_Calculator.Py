"""Odisha domestic electricity bill estimator with consumer and billing history."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import tkinter as tk
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Generator
from urllib.parse import urlsplit
from xml.sax.saxutils import escape


DISCOMS = ("TPCODL", "TPNODL", "TPWODL", "TPSODL")
CONSUMER_CATEGORIES = (
    "Domestic General",
    "Kutir Jyoti",
    "Single-phase domestic",
    "Three-phase domestic",
)
METER_TYPES = (
    "Single-phase meter",
    "Three-phase meter",
    "Smart meter",
)

# These are the values already present in the original calculator. They are
# estimates shared across DISCOMs and categories, not an official tariff table.
TARIFF_SLABS = (
    (Decimal("50"), Decimal("2.90")),
    (Decimal("200"), Decimal("4.70")),
    (Decimal("400"), Decimal("5.70")),
    (None, Decimal("6.10")),
)
FIXED_CHARGE_PER_KW = Decimal("20.00")
DEFAULT_METER_RENT = Decimal("20.00")
DEFAULT_DUTY_PERCENT = Decimal("5.00")
DEFAULT_OTHER_CHARGES = Decimal("100.00")

METER_RENT_BY_TYPE = {
    "Single-phase meter": DEFAULT_METER_RENT,
    "Three-phase meter": DEFAULT_METER_RENT,
    "Smart meter": DEFAULT_METER_RENT,
}
MONEY = Decimal("0.01")
DATABASE_PATH = Path(__file__).with_name("bill_calculator.sqlite3")
PASSWORD_HASH_ITERATIONS = 310_000


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return (
        f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}"
        f"${salt.hex()}${password_hash.hex()}"
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, expected_hash = stored_hash.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if not 1 <= iterations <= 1_000_000:
            return False
        salt = bytes.fromhex(salt_text)
        expected = bytes.fromhex(expected_hash)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


class PaymentApiError(RuntimeError):
    """A safe-to-display error from the payment backend."""


def _payment_api_request(
    method: str,
    endpoint: str,
    payload: dict[str, object] | None = None,
    access_token: str | None = None,
) -> dict[str, object]:
    base_url = os.environ.get("BILL_CALCULATOR_API_URL", "").strip().rstrip("/")
    if not base_url:
        raise PaymentApiError("BILL_CALCULATOR_API_URL is not configured.")
    parsed_url = urlsplit(base_url)
    if (
        not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or (
            parsed_url.scheme != "https"
            and not (
                parsed_url.scheme == "http"
                and parsed_url.hostname.lower() in ("localhost", "127.0.0.1", "::1")
            )
        )
    ):
        raise PaymentApiError(
            "The payment API must use HTTPS, except for a local development server."
        )

    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(
        f"{base_url}{endpoint}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read()
    except urllib.error.HTTPError as error:
        try:
            error_body = json.loads(error.read())
        except (json.JSONDecodeError, UnicodeDecodeError):
            error_body = {}
        detail = error_body.get("detail")
        raise PaymentApiError(
            str(detail) if isinstance(detail, str) else "Payment service request failed."
        ) from error
    except urllib.error.URLError as error:
        raise PaymentApiError("Could not connect to the payment service.") from error
    try:
        result = json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise PaymentApiError("Payment service returned an invalid response.") from error
    if not isinstance(result, dict):
        raise PaymentApiError("Payment service returned an invalid response.")
    return result


def _decimal(value: Decimal | float | int | str, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{label} must be a number.") from error
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    return result


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def calculate_energy_charge(units: Decimal | float | int | str) -> Decimal:
    """Calculate progressive slab charges for non-negative consumption."""
    remaining = _decimal(units, "Units")
    if remaining < 0:
        raise ValueError("Units cannot be negative.")

    charge = Decimal("0")
    previous_limit = Decimal("0")
    for limit, rate in TARIFF_SLABS:
        if remaining <= 0:
            break
        slab_units = remaining if limit is None else min(
            remaining, limit - previous_limit
        )
        charge += slab_units * rate
        remaining -= slab_units
        if limit is not None:
            previous_limit = limit
    return _money(charge)


@dataclass(frozen=True)
class BillBreakdown:
    units: Decimal
    energy_charge: Decimal
    fixed_charge: Decimal
    meter_rent: Decimal
    electricity_duty: Decimal
    other_charges: Decimal
    prompt_payment_rebate: Decimal
    delayed_payment_surcharge: Decimal
    total: Decimal


def calculate_bill_amount(
    previous_reading: Decimal | float | int | str,
    current_reading: Decimal | float | int | str,
    sanctioned_load: Decimal | float | int | str,
    meter_multiplier: Decimal | float | int | str,
    meter_rent: Decimal | float | int | str,
    duty_percent: Decimal | float | int | str,
    rebate_percent: Decimal | float | int | str,
    surcharge_percent: Decimal | float | int | str,
    other_charges: Decimal | float | int | str = 0,
    due_date: date | None = None,
    payment_date: date | None = None,
) -> BillBreakdown:
    """Calculate a bill using the configured estimate and user-entered rules."""
    previous = _decimal(previous_reading, "Previous reading")
    current = _decimal(current_reading, "Current reading")
    load = _decimal(sanctioned_load, "Sanctioned load")
    multiplier = _decimal(meter_multiplier, "Meter multiplier")
    rent = _decimal(meter_rent, "Meter rent")
    duty_rate = _decimal(duty_percent, "Electricity duty")
    rebate_rate = _decimal(rebate_percent, "Prompt-payment rebate")
    surcharge_rate = _decimal(surcharge_percent, "Delayed-payment surcharge")
    other = _decimal(other_charges, "Other charges")

    if current < previous:
        raise ValueError("Current reading cannot be less than previous reading.")
    if load <= 0:
        raise ValueError("Sanctioned load must be positive.")
    if multiplier <= 0:
        raise ValueError("Meter multiplier must be positive.")
    if min(rent, duty_rate, rebate_rate, surcharge_rate, other) < 0:
        raise ValueError("Rates and charges cannot be negative.")
    if payment_date is not None and due_date is None:
        raise ValueError("Enter a due date before applying a payment adjustment.")

    units = (current - previous) * multiplier
    energy_charge = calculate_energy_charge(units)
    fixed_charge = _money(
        load.to_integral_value(rounding=ROUND_CEILING) * FIXED_CHARGE_PER_KW
    )
    rent = _money(rent)
    other = _money(other)
    duty = _money((energy_charge + fixed_charge) * duty_rate / Decimal("100"))
    gross = energy_charge + fixed_charge + rent + duty + other

    rebate = Decimal("0.00")
    surcharge = Decimal("0.00")
    if payment_date is not None and due_date is not None:
        if payment_date <= due_date:
            rebate = _money(gross * rebate_rate / Decimal("100"))
        else:
            surcharge = _money(gross * surcharge_rate / Decimal("100"))

    total = max(Decimal("0.00"), _money(gross - rebate + surcharge))
    return BillBreakdown(
        units=units,
        energy_charge=energy_charge,
        fixed_charge=fixed_charge,
        meter_rent=rent,
        electricity_duty=duty,
        other_charges=other,
        prompt_payment_rebate=rebate,
        delayed_payment_surcharge=surcharge,
        total=total,
    )


class BillCalculatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Odisha Electricity Bill Estimator")
        self.root.geometry("1020x720")
        self.root.minsize(860, 600)
        self.role: str | None = None
        self.consumer_number: str | None = None
        self.selected_bill_id: int | None = None
        self.payment_api_token: str | None = None
        self.payment_api_error = (
            "Configure BILL_CALCULATOR_API_URL to enable verified PhonePe payments."
        )
        self.active_payment_order_id: str | None = None
        self.payment_poll_count = 0
        self._initialize_database()
        self._build_login_ui()

    def _clear_window(self) -> None:
        animation_id = getattr(self, "login_animation_id", None)
        if animation_id is not None:
            try:
                self.root.after_cancel(animation_id)
            except tk.TclError:
                pass
            self.login_animation_id = None
        for widget in self.root.winfo_children():
            widget.destroy()

    def _build_login_ui(self) -> None:
        self._clear_window()
        self.root.configure(background="#081323")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Login.TFrame", background="#081323")
        style.configure("LoginCard.TFrame", background="#12243a")
        style.configure(
            "Login.TLabel",
            background="#12243a",
            foreground="#e7f0ff",
            font=("Segoe UI", 10),
        )
        style.configure(
            "LoginTitle.TLabel",
            background="#12243a",
            foreground="#f2f7ff",
            font=("Segoe UI", 22, "bold"),
        )
        style.configure(
            "LoginSubtitle.TLabel",
            background="#12243a",
            foreground="#9db3cb",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Login.TNotebook",
            background="#12243a",
            borderwidth=0,
        )
        style.configure(
            "Login.TNotebook.Tab",
            background="#1a304a",
            foreground="#c9d8e9",
            padding=(18, 10),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Login.TNotebook.Tab",
            background=[("selected", "#2463a5")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Login.TButton",
            background="#1689d4",
            foreground="#ffffff",
            padding=(16, 9),
            font=("Segoe UI", 10, "bold"),
        )
        style.map(
            "Login.TButton",
            background=[("active", "#31a8ed"), ("disabled", "#405168")],
            foreground=[("disabled", "#a9b5c3")],
        )

        container = ttk.Frame(self.root, style="Login.TFrame", padding=34)
        container.pack(fill="both", expand=True)
        card = ttk.Frame(container, style="LoginCard.TFrame", padding=28)
        card.pack(fill="both", expand=True, padx=75, pady=18)
        self.login_banner = tk.Canvas(
            card,
            height=64,
            background="#12243a",
            highlightthickness=0,
        )
        self.login_banner.pack(fill="x", pady=(0, 8))
        self.login_banner.create_oval(
            12, 12, 52, 52, outline="#1c95dc", width=2
        )
        self.login_pulse = self.login_banner.create_oval(
            24, 24, 40, 40, fill="#51d6ff", outline=""
        )
        self.login_banner.create_text(
            72,
            25,
            anchor="w",
            text="POWER PORTAL",
            fill="#51d6ff",
            font=("Segoe UI", 9, "bold"),
        )
        self.login_banner.create_text(
            72,
            45,
            anchor="w",
            text="Odisha Electricity Bill Estimator",
            fill="#f2f7ff",
            font=("Segoe UI", 16, "bold"),
        )
        self.login_phase = 0
        self._animate_login()

        ttk.Label(
            card,
            text="Welcome back",
            style="LoginTitle.TLabel",
        ).pack(anchor="w", pady=(4, 2))
        ttk.Label(
            card,
            text="Sign in to calculate, review and manage electricity bills.",
            style="LoginSubtitle.TLabel",
        ).pack(anchor="w", pady=(0, 16))

        self.captcha_answers: dict[str, int] = {}
        self.captcha_questions: dict[str, tk.StringVar] = {}
        self.captcha_inputs: dict[str, tk.StringVar] = {}
        self.login_tabs = ttk.Notebook(card, style="Login.TNotebook")
        self.login_tabs.pack(fill="both", expand=True)
        admin_frame = ttk.Frame(self.login_tabs, style="LoginCard.TFrame", padding=18)
        self.consumer_login_frame = ttk.Frame(
            self.login_tabs, style="LoginCard.TFrame", padding=18
        )
        self.consumer_register_frame = ttk.Frame(
            self.login_tabs, style="LoginCard.TFrame", padding=18
        )
        self.login_tabs.add(admin_frame, text="Administrator")
        self.login_tabs.add(self.consumer_login_frame, text="Consumer login")
        self.login_tabs.add(self.consumer_register_frame, text="Create consumer login")

        self.admin_username = tk.StringVar()
        self.admin_password = tk.StringVar()
        self._add_login_field(admin_frame, "Username", self.admin_username, 0)
        admin_password_entry = self._add_login_field(
            admin_frame, "Password", self.admin_password, 1, hidden=True
        )
        self._add_captcha(admin_frame, "admin", 2)
        ttk.Button(
            admin_frame,
            text="Sign in as administrator",
            style="Login.TButton",
            command=self._login_admin,
        ).grid(row=3, column=1, sticky="e", padx=8, pady=(12, 8))
        admin_password_entry.bind("<Return>", lambda _event: self._login_admin())

        with self._connect() as connection:
            has_admin = connection.execute(
                "SELECT 1 FROM admin_accounts LIMIT 1"
            ).fetchone()
        if not has_admin:
            setup = ttk.LabelFrame(
                admin_frame,
                text="First run: create the administrator account",
                padding=10,
            )
            setup.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            self.setup_username = tk.StringVar()
            self.setup_password = tk.StringVar()
            self.setup_password_confirm = tk.StringVar()
            self._add_login_field(setup, "Username", self.setup_username, 0)
            self._add_login_field(
                setup, "Password (8+ characters)", self.setup_password, 1, hidden=True
            )
            confirm_entry = self._add_login_field(
                setup, "Confirm password", self.setup_password_confirm, 2, hidden=True
            )
            ttk.Button(
                setup,
                text="Create administrator",
                style="Login.TButton",
                command=self._create_initial_admin,
            ).grid(row=3, column=1, sticky="e", padx=8, pady=(8, 2))
            confirm_entry.bind(
                "<Return>", lambda _event: self._create_initial_admin()
            )
        self._refresh_captcha("admin")

        self.consumer_username = tk.StringVar()
        self.consumer_password = tk.StringVar()
        self._add_login_field(
            self.consumer_login_frame, "Consumer ID", self.consumer_username, 0
        )
        consumer_password_entry = self._add_login_field(
            self.consumer_login_frame,
            "Password",
            self.consumer_password,
            1,
            hidden=True,
        )
        self._add_captcha(self.consumer_login_frame, "consumer_login", 2)
        ttk.Button(
            self.consumer_login_frame,
            text="Sign in",
            style="Login.TButton",
            command=self._login_consumer,
        ).grid(row=3, column=1, sticky="e", padx=8, pady=(12, 8))
        consumer_password_entry.bind(
            "<Return>", lambda _event: self._login_consumer()
        )

        self.registration_consumer_number = tk.StringVar()
        self.registration_code = tk.StringVar()
        self.registration_password = tk.StringVar()
        self.registration_password_confirm = tk.StringVar()
        self._add_login_field(
            self.consumer_register_frame,
            "Consumer ID (provided by admin)",
            self.registration_consumer_number,
            0,
        )
        self._add_login_field(
            self.consumer_register_frame,
            "One-time code (from admin)",
            self.registration_code,
            1,
        )
        self._add_login_field(
            self.consumer_register_frame,
            "Create password (8+ characters)",
            self.registration_password,
            2,
            hidden=True,
        )
        register_confirm_entry = self._add_login_field(
            self.consumer_register_frame,
            "Confirm password",
            self.registration_password_confirm,
            3,
            hidden=True,
        )
        self._add_captcha(self.consumer_register_frame, "consumer_register", 4)
        ttk.Button(
            self.consumer_register_frame,
            text="Create my login",
            style="Login.TButton",
            command=self._register_consumer,
        ).grid(row=5, column=1, sticky="e", padx=8, pady=(12, 8))
        register_confirm_entry.bind(
            "<Return>", lambda _event: self._register_consumer()
        )
        self._refresh_captcha("consumer_login")
        self._refresh_captcha("consumer_register")

    def _add_login_field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        hidden: bool = False,
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, style="Login.TLabel").grid(
            row=row, column=0, sticky="w", padx=8, pady=7
        )
        entry = ttk.Entry(
            parent,
            textvariable=variable,
            show="*" if hidden else "",
            width=34,
        )
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=7)
        parent.columnconfigure(1, weight=1)
        return entry

    def _add_captcha(self, parent: ttk.Frame, key: str, row: int) -> None:
        ttk.Label(parent, text="CAPTCHA", style="Login.TLabel").grid(
            row=row, column=0, sticky="w", padx=8, pady=7
        )
        captcha_frame = ttk.Frame(parent, style="LoginCard.TFrame")
        captcha_frame.grid(row=row, column=1, sticky="ew", padx=8, pady=7)
        question = tk.StringVar()
        response = tk.StringVar()
        self.captcha_questions[key] = question
        self.captcha_inputs[key] = response
        ttk.Label(
            captcha_frame, textvariable=question, style="Login.TLabel"
        ).pack(side="left", padx=(0, 10))
        ttk.Entry(captcha_frame, textvariable=response, width=8).pack(side="left")
        ttk.Button(
            captcha_frame,
            text="New",
            command=lambda: self._refresh_captcha(key),
        ).pack(side="left", padx=(8, 0))

    def _refresh_captcha(self, key: str) -> None:
        first = secrets.randbelow(9) + 1
        second = secrets.randbelow(9) + 1
        self.captcha_answers[key] = first + second
        self.captcha_questions[key].set(f"{first} + {second} = ?")
        self.captcha_inputs[key].set("")

    def _verify_captcha(self, key: str) -> bool:
        is_valid = self.captcha_inputs[key].get().strip() == str(
            self.captcha_answers[key]
        )
        self._refresh_captcha(key)
        if not is_valid:
            messagebox.showerror("CAPTCHA required", "Enter the correct CAPTCHA answer.")
        return is_valid

    def _create_consumer_account(
        self, consumer_number: str, registration_code: str, password: str
    ) -> None:
        if len(password) < 8:
            raise ValueError("Use a password with at least 8 characters.")
        with self._connect() as connection:
            consumer = connection.execute(
                "SELECT 1 FROM consumers WHERE consumer_number=?",
                (consumer_number,),
            ).fetchone()
            if consumer is None:
                raise ValueError(
                    "Ask the administrator to create your consumer ID first."
                )
            account = connection.execute(
                "SELECT 1 FROM consumer_accounts WHERE consumer_number=?",
                (consumer_number,),
            ).fetchone()
            if account is not None:
                raise ValueError(
                    "This consumer ID already has a login. Sign in or contact the administrator."
                )
            enrollment = connection.execute(
                """
                SELECT code_hash, expires_at
                FROM consumer_registration_codes
                WHERE consumer_number=?
                """,
                (consumer_number,),
            ).fetchone()
            supplied_code_hash = hashlib.sha256(
                registration_code.strip().encode("utf-8")
            ).hexdigest()
            if (
                enrollment is None
                or enrollment[1] <= time.time()
                or not hmac.compare_digest(
                    supplied_code_hash, enrollment[0]
                )
            ):
                raise ValueError(
                    "The one-time registration code is invalid or expired. "
                    "Ask the administrator to issue a new code."
                )
            connection.execute(
                """
                INSERT INTO consumer_accounts (consumer_number, password_hash)
                VALUES (?, ?)
                """,
                (consumer_number, _hash_password(password)),
            )
            connection.execute(
                "DELETE FROM consumer_registration_codes WHERE consumer_number=?",
                (consumer_number,),
            )

    def _animate_login(self) -> None:
        if not self.login_banner.winfo_exists():
            return
        self.login_phase = (self.login_phase + 1) % 12
        radius = 5 + min(self.login_phase, 12 - self.login_phase)
        self.login_banner.coords(
            self.login_pulse,
            32 - radius,
            32 - radius,
            32 + radius,
            32 + radius,
        )
        self.login_banner.itemconfigure(
            self.login_pulse,
            fill="#51d6ff" if self.login_phase < 6 else "#76a9ff",
        )
        self.login_animation_id = self.root.after(140, self._animate_login)

    def _register_consumer(self) -> None:
        if not self._verify_captcha("consumer_register"):
            return
        consumer_number = self.registration_consumer_number.get().strip()
        registration_code = self.registration_code.get().strip()
        password = self.registration_password.get()
        if not consumer_number:
            messagebox.showerror("Missing information", "Enter the consumer ID provided by your administrator.")
            return
        if password != self.registration_password_confirm.get():
            messagebox.showerror("Password mismatch", "The passwords do not match.")
            return
        try:
            self._create_consumer_account(
                consumer_number, registration_code, password
            )
            self.consumer_username.set(consumer_number)
            self.registration_code.set("")
            self.registration_password.set("")
            self.registration_password_confirm.set("")
            self.login_tabs.select(self.consumer_login_frame)
            messagebox.showinfo(
                "Registration complete", "Your consumer login is ready."
            )
        except ValueError as error:
            messagebox.showerror("Registration failed", str(error))
        except sqlite3.IntegrityError:
            messagebox.showerror(
                "Already registered",
                "This consumer ID already has a login. Sign in or contact the administrator.",
            )
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))

    def _create_initial_admin(self) -> None:
        username = self.setup_username.get().strip()
        password = self.setup_password.get()
        if not username:
            messagebox.showerror("Missing information", "Enter an administrator username.")
            return
        if len(password) < 8:
            messagebox.showerror(
                "Weak password", "Use a password with at least 8 characters."
            )
            return
        if password != self.setup_password_confirm.get():
            messagebox.showerror("Password mismatch", "The passwords do not match.")
            return
        try:
            with self._connect() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM admin_accounts LIMIT 1"
                ).fetchone()
                if exists:
                    messagebox.showerror(
                        "Administrator exists",
                        "An administrator account has already been created.",
                    )
                    self._build_login_ui()
                    return
                connection.execute(
                    "INSERT INTO admin_accounts (username, password_hash) VALUES (?, ?)",
                    (username, _hash_password(password)),
                )
            self.admin_username.set(username)
            self.admin_password.set("")
            messagebox.showinfo(
                "Administrator created", "Log in with the new administrator account."
            )
            self._build_login_ui()
            self.admin_username.set(username)
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))

    def _login_admin(self) -> None:
        if not self._verify_captcha("admin"):
            return
        username = self.admin_username.get().strip()
        password = self.admin_password.get()
        try:
            with self._connect() as connection:
                account = connection.execute(
                    "SELECT password_hash FROM admin_accounts WHERE username=?",
                    (username,),
                ).fetchone()
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))
            return
        if account is None or not _verify_password(password, account[0]):
            messagebox.showerror("Login failed", "Invalid administrator username or password.")
            return
        self.admin_password.set("")
        self._open_application("admin")

    def _login_consumer(self) -> None:
        if not self._verify_captcha("consumer_login"):
            return
        consumer_number = self.consumer_username.get().strip()
        password = self.consumer_password.get()
        try:
            with self._connect() as connection:
                account = connection.execute(
                    """
                    SELECT password_hash FROM consumer_accounts
                    WHERE consumer_number=?
                    """,
                    (consumer_number,),
                ).fetchone()
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))
            return
        if account is None or not _verify_password(password, account[0]):
            messagebox.showerror("Login failed", "Invalid consumer ID or password.")
            return
        self.payment_api_token = None
        self.payment_api_error = (
            "Configure BILL_CALCULATOR_API_URL to enable verified PhonePe payments."
        )
        if os.environ.get("BILL_CALCULATOR_API_URL"):
            try:
                api_login = _payment_api_request(
                    "POST",
                    "/api/auth/login",
                    {
                        "role": "consumer",
                        "username": consumer_number,
                        "password": password,
                    },
                )
                token = api_login.get("access_token")
                if isinstance(token, str):
                    self.payment_api_token = token
                    self.payment_api_error = ""
                else:
                    self.payment_api_error = "Payment API did not return an access token."
            except PaymentApiError as error:
                self.payment_api_error = str(error)
        self.consumer_password.set("")
        self._open_application("consumer", consumer_number)

    def _open_application(
        self, role: str, consumer_number: str | None = None
    ) -> None:
        self.role = role
        self.consumer_number = consumer_number
        self.selected_bill_id = None
        self._build_ui()
        self.refresh_consumers()
        self.refresh_history()
        if role == "consumer":
            self.refresh_payments()

    def logout(self) -> None:
        revoke_error: str | None = None
        if self.payment_api_token:
            try:
                _payment_api_request(
                    "POST",
                    "/api/auth/logout",
                    access_token=self.payment_api_token,
                )
            except PaymentApiError as error:
                revoke_error = str(error)
        self.payment_api_token = None
        self.active_payment_order_id = None
        self.role = None
        self.consumer_number = None
        self.selected_bill_id = None
        self._build_login_ui()
        if revoke_error:
            messagebox.showwarning(
                "Payment session",
                f"The payment API session could not be revoked and will expire automatically:\n{revoke_error}",
            )

    def _is_admin(self) -> bool:
        if self.role != "admin":
            messagebox.showerror("Access denied", "Administrator access is required.")
            return False
        return True

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        connection = sqlite3.connect(DATABASE_PATH)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
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
                """
            )

    def _build_ui(self) -> None:
        self._clear_window()
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=12, pady=(10, 0))
        heading = ttk.Label(
            header,
            text="Odisha Electricity Bill Estimator",
            font=("Segoe UI", 18, "bold"),
        )
        heading.pack(side="left", pady=(4, 4))
        ttk.Button(header, text="Log out", command=self.logout).pack(
            side="right", padx=4
        )
        ttk.Label(
            self.root,
            text=(
                "Estimated only: sample slabs are shared across DISCOMs and "
                "categories. Meter rent, duty, and other charges use the "
                "calculator's fixed preset amounts."
            ),
            wraplength=950,
        ).pack(pady=(0, 10))

        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=8)
        self.bill_tab = ttk.Frame(self.tabs, padding=12)
        self.history_tab = ttk.Frame(self.tabs, padding=12)
        if self.role == "admin":
            self.consumer_tab = ttk.Frame(self.tabs, padding=12)
            self.tabs.add(self.consumer_tab, text="Consumer Entry")
            self._build_consumer_tab()
        self.tabs.add(self.bill_tab, text="Calculation")
        self.tabs.add(self.history_tab, text="Reports")
        self._build_bill_tab()
        self._build_history_tab()
        if self.role == "consumer":
            self.payment_tab = ttk.Frame(self.tabs, padding=12)
            self.tabs.add(self.payment_tab, text="Payment")
            self._build_payment_tab()

    def _build_consumer_tab(self) -> None:
        self.consumer_vars = {
            "consumer_number": tk.StringVar(),
            "name": tk.StringVar(),
            "discom": tk.StringVar(value=DISCOMS[0]),
            "category": tk.StringVar(value=CONSUMER_CATEGORIES[0]),
            "sanctioned_load": tk.StringVar(),
            "meter_type": tk.StringVar(value=METER_TYPES[0]),
            "meter_multiplier": tk.StringVar(value="1"),
        }
        fields = (
            ("Consumer number", "consumer_number", None),
            ("Consumer name", "name", None),
            ("DISCOM", "discom", DISCOMS),
            ("Consumer category", "category", CONSUMER_CATEGORIES),
            ("Sanctioned load (kW)", "sanctioned_load", None),
            ("Meter type", "meter_type", METER_TYPES),
            ("Meter multiplier", "meter_multiplier", None),
        )
        for row, (label, key, values) in enumerate(fields):
            ttk.Label(self.consumer_tab, text=label).grid(
                row=row, column=0, sticky="w", padx=8, pady=7
            )
            if values:
                widget = ttk.Combobox(
                    self.consumer_tab,
                    textvariable=self.consumer_vars[key],
                    values=values,
                    state="readonly",
                    width=34,
                )
            else:
                widget = ttk.Entry(
                    self.consumer_tab,
                    textvariable=self.consumer_vars[key],
                    width=37,
                )
            widget.grid(row=row, column=1, sticky="w", padx=8, pady=7)

        buttons = ttk.Frame(self.consumer_tab)
        buttons.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(buttons, text="Save Consumer", command=self.save_consumer).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(buttons, text="Clear", command=self.clear_consumer_form).pack(
            side="left"
        )
        ttk.Label(
            self.consumer_tab,
            text="Consumers use their Consumer ID to create their own password on the login page.",
            wraplength=600,
        ).grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", padx=8)

    def _build_bill_tab(self) -> None:
        self.bill_vars = {
            "consumer_number": tk.StringVar(),
            "previous_reading": tk.StringVar(value="0"),
            "current_reading": tk.StringVar(),
            "billing_date": tk.StringVar(value=date.today().isoformat()),
            "due_date": tk.StringVar(
                value=(date.today() + timedelta(days=30)).isoformat()
            ),
            "payment_date": tk.StringVar(),
            "meter_rent": tk.StringVar(value=str(DEFAULT_METER_RENT)),
            "duty_percent": tk.StringVar(value=str(DEFAULT_DUTY_PERCENT)),
            "other_charges": tk.StringVar(value=str(DEFAULT_OTHER_CHARGES)),
            "rebate_percent": tk.StringVar(value="0"),
            "surcharge_percent": tk.StringVar(value="0"),
        }
        fields = (
            ("Consumer number", "consumer_number"),
            ("Previous reading", "previous_reading"),
            ("Current reading", "current_reading"),
            ("Bill date (YYYY-MM-DD)", "billing_date"),
            ("Due date (YYYY-MM-DD)", "due_date"),
            ("Payment date (optional)", "payment_date"),
            ("Meter rent (fixed ₹)", "meter_rent"),
            ("Electricity duty (fixed %)", "duty_percent"),
            ("Other charges (fixed ₹)", "other_charges"),
            ("Prompt-payment rebate (%)", "rebate_percent"),
            ("Delayed-payment surcharge (%)", "surcharge_percent"),
        )
        for index, (label, key) in enumerate(fields):
            row, column = divmod(index, 2)
            base_column = column * 2
            ttk.Label(self.bill_tab, text=label).grid(
                row=row, column=base_column, sticky="w", padx=8, pady=6
            )
            if key == "consumer_number":
                self.bill_consumer_combo = ttk.Combobox(
                    self.bill_tab,
                    textvariable=self.bill_vars[key],
                    state="readonly",
                    width=27,
                )
                self.bill_consumer_combo.grid(
                    row=row, column=base_column + 1, sticky="w", padx=8, pady=6
                )
                self.bill_consumer_combo.bind(
                    "<<ComboboxSelected>>", self._consumer_selected
                )
            else:
                ttk.Entry(
                    self.bill_tab,
                    textvariable=self.bill_vars[key],
                    state=(
                        "readonly"
                        if key in ("meter_rent", "duty_percent", "other_charges")
                        else "normal"
                    ),
                    width=30,
                ).grid(row=row, column=base_column + 1, sticky="w", padx=8, pady=6)

        note_row = (len(fields) + 1) // 2
        ttk.Label(
            self.bill_tab,
            text=(
                "Enter a payment date to apply the configured rebate (on/before due "
                "date) or flat surcharge (after due date). Leave it blank for an "
                "unadjusted bill. Fixed calculator values: meter rent ₹20.00, "
                "electricity duty 5%, and other charges ₹100.00. Rebate and "
                "surcharge rates default to zero."
            ),
            wraplength=900,
        ).grid(row=note_row, column=0, columnspan=4, sticky="w", padx=8, pady=8)
        self.bill_result = ttk.Label(self.bill_tab, text="", justify="left")
        self.bill_result.grid(
            row=note_row + 1, column=0, columnspan=4, sticky="w", padx=8, pady=8
        )
        actions = ttk.Frame(self.bill_tab)
        actions.grid(row=note_row + 2, column=0, columnspan=4, sticky="w", pady=10)
        ttk.Button(actions, text="Calculate & Save Bill", command=self.calculate_and_save).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(actions, text="Download PDF", command=self.download_selected_pdf).pack(
            side="left", padx=8
        )
        ttk.Button(actions, text="Print Bill", command=self.print_selected_bill).pack(
            side="left", padx=8
        )

    def _build_history_tab(self) -> None:
        columns = (
            "billing_date",
            "consumer_number",
            "name",
            "discom",
            "category",
            "units",
            "total",
        )
        self.history_tree = ttk.Treeview(
            self.history_tab, columns=columns, show="headings", height=18
        )
        headings = {
            "billing_date": "Bill date",
            "consumer_number": "Consumer no.",
            "name": "Consumer",
            "discom": "DISCOM",
            "category": "Category",
            "units": "Units (kWh)",
            "total": "Amount (₹)",
        }
        widths = (100, 125, 160, 85, 155, 95, 105)
        for column, width in zip(columns, widths):
            self.history_tree.heading(column, text=headings[column])
            self.history_tree.column(column, width=width, anchor="w")
        self.history_tree.pack(fill="both", expand=True)
        self.history_tree.bind("<<TreeviewSelect>>", self._history_selected)
        ttk.Button(
            self.history_tab, text="Refresh History", command=self.refresh_history
        ).pack(anchor="w", pady=(10, 0))

    def _build_payment_tab(self) -> None:
        ttk.Label(
            self.payment_tab,
            text=f"Payment details for consumer ID: {self.consumer_number}",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            self.payment_tab,
            text=(
                "The payment backend calculates the remaining balance for each bill. "
                "PhonePe credentials stay on the backend; completed payments are "
                "confirmed by PhonePe status or its authenticated webhook."
            ),
            wraplength=850,
        ).pack(anchor="w", pady=(0, 10))
        columns = ("billing_date", "due_date", "bill_total", "due_amount", "state")
        self.payment_tree = ttk.Treeview(
            self.payment_tab, columns=columns, show="headings", height=16
        )
        for column, heading, width in (
            ("billing_date", "Bill date", 150),
            ("due_date", "Due date", 150),
            ("bill_total", "Bill total (₹)", 150),
            ("due_amount", "Active due (₹)", 150),
            ("state", "Payment state", 130),
        ):
            self.payment_tree.heading(column, text=heading)
            self.payment_tree.column(column, width=width, anchor="w")
        self.payment_tree.pack(fill="both", expand=True)
        self.payment_tree.bind("<<TreeviewSelect>>", self._payment_selected)
        actions = ttk.Frame(self.payment_tab)
        actions.pack(fill="x", pady=(10, 0))
        self.payment_status = tk.StringVar(
            value=self.payment_api_error or "Select a bill to pay its active due."
        )
        ttk.Label(actions, textvariable=self.payment_status).pack(side="left", fill="x", expand=True)
        ttk.Button(
            actions,
            text="Refresh due",
            command=self.refresh_payments,
        ).pack(side="right", padx=(8, 0))
        self.payment_pay_button = ttk.Button(
            actions,
            text="Pay active due with PhonePe UPI",
            command=self.start_payment,
            state="disabled",
        )
        self.payment_pay_button.pack(side="right")
        self.payment_selected_bill_id: int | None = None

    def _payment_selected(self, _event: object | None = None) -> None:
        selection = self.payment_tree.selection()
        if not selection:
            return
        self.payment_selected_bill_id = int(selection[0])
        values = self.payment_tree.item(selection[0], "values")
        self.payment_status.set(
            f"Bill dated {values[0]}: active due ₹{values[3]} "
            f"({values[4]})."
        )
        self.payment_pay_button.configure(
            state=(
                "normal"
                if self.payment_api_token and values[4] == "DUE"
                else "disabled"
            )
        )

    def start_payment(self) -> None:
        if self.payment_api_token is None:
            self.payment_status.set(
                self.payment_api_error
                or "Payment backend login is not available. Please sign in again."
            )
            return
        if self.payment_selected_bill_id is None:
            self.payment_status.set("Select a bill with an active due first.")
            return
        self.payment_pay_button.configure(state="disabled")
        try:
            order = _payment_api_request(
                "POST",
                "/api/payments/orders",
                {"bill_id": self.payment_selected_bill_id},
                self.payment_api_token,
            )
            redirect_url = order.get("redirect_url")
            merchant_order_id = order.get("merchant_order_id")
            redirect_host = (
                urlsplit(redirect_url).hostname
                if isinstance(redirect_url, str)
                else None
            )
            if (
                not isinstance(merchant_order_id, str)
                or not redirect_host
                or urlsplit(redirect_url).scheme != "https"
                or not (
                    redirect_host == "phonepe.com"
                    or redirect_host.endswith(".phonepe.com")
                )
            ):
                raise PaymentApiError(
                    "Payment backend returned an invalid PhonePe checkout URL."
                )
            self.active_payment_order_id = merchant_order_id
            self.payment_poll_count = 0
            self.payment_status.set(
                "PhonePe checkout opened. Waiting for verified payment status..."
            )
            if not webbrowser.open(redirect_url):
                messagebox.showwarning(
                    "Open PhonePe checkout",
                    f"Your browser could not be opened automatically.\n{redirect_url}",
                )
            self.root.after(3000, self._poll_payment_status)
        except PaymentApiError as error:
            self.payment_status.set(str(error))
            self.payment_pay_button.configure(state="normal")

    def _poll_payment_status(self) -> None:
        if not self.payment_api_token or not self.active_payment_order_id:
            return
        self.payment_poll_count += 1
        try:
            result = _payment_api_request(
                "GET",
                f"/api/payments/orders/{self.active_payment_order_id}",
                access_token=self.payment_api_token,
            )
        except PaymentApiError as error:
            self.payment_status.set(
                f"Could not refresh payment status: {error}"
            )
            if self.payment_poll_count < 120:
                self.root.after(5000, self._poll_payment_status)
            return

        state = result.get("state")
        if state == "COMPLETED":
            self.payment_status.set("Payment verified by PhonePe.")
            self.active_payment_order_id = None
            self.refresh_payments()
            messagebox.showinfo("Payment complete", "PhonePe verified this payment.")
        elif state == "FAILED":
            self.payment_status.set(
                "PhonePe reported that this payment failed. You may retry."
            )
            self.active_payment_order_id = None
            self.refresh_payments()
        elif state == "PENDING" and self.payment_poll_count < 120:
            self.payment_status.set(
                "Payment is pending. Checking PhonePe again..."
            )
            self.root.after(3000, self._poll_payment_status)
        else:
            self.payment_status.set(
                "Payment is still pending. Use Refresh due to check again."
            )

    def clear_consumer_form(self) -> None:
        for key, variable in self.consumer_vars.items():
            variable.set("1" if key == "meter_multiplier" else "")
        self.consumer_vars["discom"].set(DISCOMS[0])
        self.consumer_vars["category"].set(CONSUMER_CATEGORIES[0])
        self.consumer_vars["meter_type"].set(METER_TYPES[0])

    def save_consumer(self) -> None:
        if not self._is_admin():
            return
        values = {
            key: variable.get().strip()
            for key, variable in self.consumer_vars.items()
        }
        if not values["consumer_number"] or not values["name"]:
            messagebox.showerror("Missing information", "Enter a consumer number and name.")
            return
        try:
            load = _decimal(values["sanctioned_load"], "Sanctioned load")
            multiplier = _decimal(values["meter_multiplier"], "Meter multiplier")
            if load <= 0 or multiplier <= 0:
                raise ValueError("Sanctioned load and meter multiplier must be positive.")
            registration_code: str | None = None
            with self._connect() as connection:
                account = connection.execute(
                    "SELECT 1 FROM consumer_accounts WHERE consumer_number=?",
                    (values["consumer_number"],),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO consumers (
                        consumer_number, name, discom, category, sanctioned_load,
                        meter_type, meter_multiplier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(consumer_number) DO UPDATE SET
                        name=excluded.name, discom=excluded.discom,
                        category=excluded.category,
                        sanctioned_load=excluded.sanctioned_load,
                        meter_type=excluded.meter_type,
                        meter_multiplier=excluded.meter_multiplier
                    """,
                    (
                        values["consumer_number"],
                        values["name"],
                        values["discom"],
                        values["category"],
                        float(load),
                        values["meter_type"],
                        float(multiplier),
                    ),
                )
                if account is None:
                    registration_code = secrets.token_urlsafe(9)
                    code_hash = hashlib.sha256(
                        registration_code.encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO consumer_registration_codes (
                            consumer_number, code_hash, expires_at
                        ) VALUES (?, ?, ?)
                        ON CONFLICT(consumer_number) DO UPDATE SET
                            code_hash=excluded.code_hash,
                            expires_at=excluded.expires_at
                        """,
                        (
                            values["consumer_number"],
                            code_hash,
                            time.time() + 7 * 24 * 60 * 60,
                        ),
                    )
            self.refresh_consumers()
            self.bill_vars["consumer_number"].set(values["consumer_number"])
            self._consumer_selected()
            if registration_code:
                messagebox.showinfo(
                    "Consumer saved",
                    f"Consumer ID {values['consumer_number']} was saved.\n"
                    "Give this one-time registration code to the consumer "
                    "(it expires in 7 days):\n\n"
                    f"{registration_code}",
                )
            else:
                messagebox.showinfo("Saved", "Consumer record saved.")
        except ValueError as error:
            messagebox.showerror("Invalid information", str(error))
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))

    def refresh_consumers(self) -> None:
        with self._connect() as connection:
            if self.role == "consumer":
                rows = connection.execute(
                    "SELECT consumer_number FROM consumers WHERE consumer_number=?",
                    (self.consumer_number,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT consumer_number FROM consumers ORDER BY consumer_number"
                ).fetchall()
        if hasattr(self, "bill_consumer_combo"):
            self.bill_consumer_combo["values"] = [row[0] for row in rows]
            if self.role == "consumer":
                self.bill_vars["consumer_number"].set(self.consumer_number or "")
                self._consumer_selected()

    def _consumer_selected(self, _event: object | None = None) -> None:
        consumer_number = self.bill_vars["consumer_number"].get()
        if not consumer_number:
            return
        if self.role == "consumer" and consumer_number != self.consumer_number:
            messagebox.showerror("Access denied", "You can only view your own account.")
            self.bill_vars["consumer_number"].set(self.consumer_number or "")
            return
        try:
            with self._connect() as connection:
                consumer = connection.execute(
                    """
                    SELECT meter_type, meter_multiplier
                    FROM consumers WHERE consumer_number=?
                    """,
                    (consumer_number,),
                ).fetchone()
                last_reading = connection.execute(
                    """
                    SELECT current_reading FROM bills
                    WHERE consumer_number=?
                    ORDER BY billing_date DESC, id DESC LIMIT 1
                    """,
                    (consumer_number,),
                ).fetchone()
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))
            return
        if consumer:
            self.bill_vars["meter_rent"].set(
                str(METER_RENT_BY_TYPE.get(consumer[0], Decimal("0.00")))
            )
            self.bill_vars["previous_reading"].set(
                str(last_reading[0] if last_reading else 0)
            )

    def calculate_and_save(self) -> None:
        consumer_number = self.bill_vars["consumer_number"].get().strip()
        if not consumer_number:
            messagebox.showerror("Missing information", "Select a consumer first.")
            return
        if self.role == "consumer" and consumer_number != self.consumer_number:
            messagebox.showerror("Access denied", "You can only calculate bills for your ID.")
            return
        try:
            billing_date = date.fromisoformat(self.bill_vars["billing_date"].get().strip())
            due_date = date.fromisoformat(self.bill_vars["due_date"].get().strip())
            payment_text = self.bill_vars["payment_date"].get().strip()
            payment_date = date.fromisoformat(payment_text) if payment_text else None
            if due_date < billing_date:
                raise ValueError("Due date cannot be before the bill date.")

            with self._connect() as connection:
                consumer = connection.execute(
                    """
                    SELECT sanctioned_load, meter_multiplier
                    FROM consumers WHERE consumer_number=?
                    """,
                    (consumer_number,),
                ).fetchone()
            if consumer is None:
                raise ValueError("Selected consumer was not found.")

            previous = self.bill_vars["previous_reading"].get().strip()
            current = self.bill_vars["current_reading"].get().strip()
            meter_rent = self.bill_vars["meter_rent"].get().strip()
            duty_percent = self.bill_vars["duty_percent"].get().strip()
            other_charges = self.bill_vars["other_charges"].get().strip()
            rebate_percent = self.bill_vars["rebate_percent"].get().strip()
            surcharge_percent = self.bill_vars["surcharge_percent"].get().strip()
            result = calculate_bill_amount(
                previous,
                current,
                consumer[0],
                consumer[1],
                meter_rent,
                duty_percent,
                rebate_percent,
                surcharge_percent,
                other_charges,
                due_date,
                payment_date,
            )
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO bills (
                        consumer_number, billing_date, due_date, payment_date,
                        previous_reading, current_reading, units, energy_charge,
                        fixed_charge, meter_rent, duty_percent, electricity_duty,
                        other_charges, rebate_percent, prompt_payment_rebate,
                        surcharge_percent, delayed_payment_surcharge, total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        consumer_number,
                        billing_date.isoformat(),
                        due_date.isoformat(),
                        payment_date.isoformat() if payment_date else None,
                        float(_decimal(previous, "Previous reading")),
                        float(_decimal(current, "Current reading")),
                        float(result.units),
                        float(result.energy_charge),
                        float(result.fixed_charge),
                        float(result.meter_rent),
                        float(_decimal(duty_percent, "Electricity duty")),
                        float(result.electricity_duty),
                        float(_decimal(other_charges, "Other charges")),
                        float(_decimal(rebate_percent, "Prompt-payment rebate")),
                        float(result.prompt_payment_rebate),
                        float(_decimal(surcharge_percent, "Delayed-payment surcharge")),
                        float(result.delayed_payment_surcharge),
                        float(result.total),
                    ),
                )
                self.selected_bill_id = cursor.lastrowid
            self.bill_result.configure(
                text=(
                    f"Consumption: {result.units} kWh    "
                    f"Energy: ₹{result.energy_charge:.2f}    "
                    f"Fixed: ₹{result.fixed_charge:.2f}    "
                    f"Rent: ₹{result.meter_rent:.2f}    "
                    f"Duty: ₹{result.electricity_duty:.2f}\n"
                    f"Other: ₹{result.other_charges:.2f}    "
                    f"Rebate: ₹{result.prompt_payment_rebate:.2f}    "
                    f"Surcharge: ₹{result.delayed_payment_surcharge:.2f}    "
                    f"TOTAL: ₹{result.total:.2f}"
                )
            )
            self.refresh_history()
            if self.role == "consumer":
                self.refresh_payments()
            messagebox.showinfo("Bill saved", "Calculated bill saved to monthly history.")
        except (ValueError, InvalidOperation) as error:
            messagebox.showerror("Invalid bill details", str(error))
        except sqlite3.Error as error:
            messagebox.showerror("Database error", str(error))

    def refresh_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        with self._connect() as connection:
            if self.role == "consumer":
                rows = connection.execute(
                    """
                    SELECT b.id, b.billing_date, c.consumer_number, c.name,
                           c.discom, c.category, b.units, b.total
                    FROM bills b JOIN consumers c USING (consumer_number)
                    WHERE b.consumer_number=?
                    ORDER BY b.billing_date DESC, b.id DESC
                    """,
                    (self.consumer_number,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT b.id, b.billing_date, c.consumer_number, c.name,
                           c.discom, c.category, b.units, b.total
                    FROM bills b JOIN consumers c USING (consumer_number)
                    ORDER BY b.billing_date DESC, b.id DESC
                    """
                ).fetchall()
        for row in rows:
            self.history_tree.insert(
                "",
                "end",
                iid=str(row[0]),
                values=(
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    f"{row[6]:.2f}",
                    f"{row[7]:.2f}",
                ),
            )

    def refresh_payments(self) -> None:
        if not hasattr(self, "payment_tree"):
            return
        for item in self.payment_tree.get_children():
            self.payment_tree.delete(item)
        self.payment_selected_bill_id = None
        self.payment_pay_button.configure(state="disabled")
        if self.payment_api_token:
            try:
                response = _payment_api_request(
                    "GET",
                    "/api/payments/due",
                    access_token=self.payment_api_token,
                )
            except PaymentApiError as error:
                self.payment_status.set(f"Payment backend unavailable: {error}")
                return
            bills = response.get("bills")
            if not isinstance(bills, list):
                self.payment_status.set(
                    "Payment backend returned an invalid due list."
                )
                return
            for bill in bills:
                if not isinstance(bill, dict):
                    continue
                bill_id = bill.get("bill_id")
                billing_date = bill.get("billing_date")
                due_date = bill.get("due_date")
                bill_total = bill.get("bill_amount_paise")
                due_amount = bill.get("due_amount_paise")
                if not all(
                    isinstance(value, int)
                    for value in (bill_id, bill_total, due_amount)
                ):
                    continue
                self.payment_tree.insert(
                    "",
                    "end",
                    iid=str(bill_id),
                    values=(
                        billing_date,
                        due_date,
                        f"{bill_total / 100:.2f}",
                        f"{due_amount / 100:.2f}",
                        "DUE",
                    ),
                )
            self.payment_status.set(
                "Active balances are calculated by the backend. Select a bill to pay."
            )
            return

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, billing_date, due_date, total
                FROM bills
                WHERE consumer_number=?
                ORDER BY billing_date DESC, id DESC
                """,
                (self.consumer_number,),
            ).fetchall()
        for row in rows:
            self.payment_tree.insert(
                "",
                "end",
                iid=str(row[0]),
                values=(
                    row[1],
                    row[2],
                    f"{row[3]:.2f}",
                    f"{row[3]:.2f}",
                    "Backend unavailable",
                ),
            )
        self.payment_status.set(self.payment_api_error)

    def _history_selected(self, _event: object | None = None) -> None:
        selection = self.history_tree.selection()
        if selection:
            self.selected_bill_id = int(selection[0])

    def _selected_bill_data(self) -> tuple[object, ...]:
        if self.selected_bill_id is None:
            raise ValueError("Calculate a bill or select a bill in Monthly History first.")
        query = """
            SELECT b.id, b.billing_date, b.due_date, b.payment_date,
                   c.consumer_number, c.name, c.discom, c.category,
                   c.sanctioned_load, c.meter_type, b.previous_reading,
                   b.current_reading, b.units, b.energy_charge, b.fixed_charge,
                   b.meter_rent, b.duty_percent, b.electricity_duty,
                   b.other_charges, b.rebate_percent, b.prompt_payment_rebate,
                   b.surcharge_percent, b.delayed_payment_surcharge, b.total
            FROM bills b JOIN consumers c USING (consumer_number)
            WHERE b.id=?
        """
        parameters: tuple[object, ...] = (self.selected_bill_id,)
        if self.role == "consumer":
            query += " AND b.consumer_number=?"
            parameters += (self.consumer_number,)
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            raise ValueError("The selected bill no longer exists.")
        return row

    @staticmethod
    def _write_pdf(path: str | Path, bill: tuple[object, ...]) -> None:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        (
            _bill_id, billing_date, due_date, payment_date, consumer_number, name,
            discom, category, load, meter_type, previous, current, units, energy,
            fixed, rent, duty_percent, duty, other, rebate_percent, rebate,
            surcharge_percent, surcharge, total,
        ) = bill
        styles = getSampleStyleSheet()
        document = SimpleDocTemplate(
            str(path),
            pagesize=A4,
            rightMargin=20 * mm,
            leftMargin=20 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=f"Electricity bill estimate - {consumer_number}",
        )
        story = [
            Paragraph("ODISHA ELECTRICITY BILL ESTIMATE", styles["Title"]),
            Paragraph(
                "Estimated bill only - not an official DISCOM-generated bill.",
                styles["Heading3"],
            ),
            Spacer(1, 8 * mm),
        ]
        details = [
            ["Consumer name", escape(str(name)), "Consumer number", escape(str(consumer_number))],
            ["DISCOM", escape(str(discom)), "Category", escape(str(category))],
            ["Bill date", str(billing_date), "Due date", str(due_date)],
            ["Payment date", str(payment_date or "Not entered"), "Sanctioned load", f"{load} kW"],
            ["Meter type", escape(str(meter_type)), "Consumption", f"{units:.2f} kWh"],
            ["Previous reading", f"{previous:.2f}", "Current reading", f"{current:.2f}"],
        ]
        table = Table(details, colWidths=(36 * mm, 46 * mm, 33 * mm, 55 * mm))
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
                    ("BACKGROUND", (2, 0), (2, -1), colors.whitesmoke),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("PADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.extend([table, Spacer(1, 8 * mm)])
        charges = [
            ["Charge description", "Amount (Rs.)"],
            ["Slab-wise energy charge", f"{energy:.2f}"],
            ["Fixed charge", f"{fixed:.2f}"],
            ["Meter rent", f"{rent:.2f}"],
            [f"Electricity duty ({duty_percent}%)", f"{duty:.2f}"],
            ["Other charges", f"{other:.2f}"],
            [f"Prompt-payment rebate ({rebate_percent}%)", f"-{rebate:.2f}"],
            [f"Delayed-payment surcharge ({surcharge_percent}%)", f"{surcharge:.2f}"],
            ["TOTAL AMOUNT", f"Rs. {total:.2f}"],
        ]
        charges_table = Table(charges, colWidths=(125 * mm, 45 * mm))
        charges_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#174a73")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf2f8")),
                    ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                    ("PADDING", (0, 0), (-1, -1), 7),
                ]
            )
        )
        story.extend(
            [
                charges_table,
                Spacer(1, 8 * mm),
                Paragraph(
                    "Tariff basis: existing sample slabs (Rs. 2.90/4.70/5.70/6.10 "
                    "per unit) and Rs. 20 per kW or part thereof. Calculator presets "
                    "also include Rs. 20 meter rent, 5% electricity duty, and Rs. 100 "
                    "other charges. The estimate is shared across DISCOMs/categories; "
                    "confirm applicable OERC/DISCOM rates before relying on it.",
                    styles["BodyText"],
                ),
            ]
        )
        document.build(story)

    def download_selected_pdf(self) -> None:
        try:
            bill = self._selected_bill_data()
            default_name = f"bill-{bill[1]}.pdf"
            path = filedialog.asksaveasfilename(
                title="Save bill as PDF",
                initialfile=default_name,
                defaultextension=".pdf",
                filetypes=(("PDF files", "*.pdf"),),
            )
            if not path:
                return
            self._write_pdf(path, bill)
            messagebox.showinfo("PDF saved", f"Bill saved to:\n{path}")
        except (ValueError, OSError, ImportError, UnicodeEncodeError) as error:
            messagebox.showerror("PDF export failed", str(error))

    def print_selected_bill(self) -> None:
        try:
            bill = self._selected_bill_data()
            file_descriptor, path = tempfile.mkstemp(
                prefix="bill-", suffix=".pdf"
            )
            os.close(file_descriptor)
            self._write_pdf(path, bill)
            if sys.platform != "win32":
                raise OSError("Direct printing is supported on Windows.")
            os.startfile(path, "print")
            messagebox.showinfo(
                "Print request sent",
                f"The bill was sent to the default PDF print handler.\nTemporary PDF: {path}",
            )
        except (ValueError, OSError, ImportError, UnicodeEncodeError) as error:
            messagebox.showerror("Print failed", str(error))


def main() -> None:
    root = tk.Tk()
    try:
        BillCalculatorApp(root)
    except sqlite3.Error as error:
        messagebox.showerror("Database error", str(error))
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
