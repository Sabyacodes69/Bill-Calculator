import hashlib
import importlib.machinery
import importlib.util
import sys
import tempfile
import time
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("Bill Calculator.Py")
LOADER = importlib.machinery.SourceFileLoader("bill_calculator", str(MODULE_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
bill_calculator = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = bill_calculator
LOADER.exec_module(bill_calculator)


class EnergyChargeTests(unittest.TestCase):
    def test_progressive_slab_boundaries(self):
        self.assertEqual(
            bill_calculator.calculate_energy_charge(50), Decimal("145.00")
        )
        self.assertEqual(
            bill_calculator.calculate_energy_charge(200), Decimal("850.00")
        )
        self.assertEqual(
            bill_calculator.calculate_energy_charge(400), Decimal("1990.00")
        )

    def test_negative_units_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            bill_calculator.calculate_energy_charge(-1)


class BillCalculationTests(unittest.TestCase):
    def test_fixed_calculator_presets_are_included_in_total(self):
        result = bill_calculator.calculate_bill_amount(
            previous_reading=0,
            current_reading=0,
            sanctioned_load=1,
            meter_multiplier=1,
            meter_rent=bill_calculator.DEFAULT_METER_RENT,
            duty_percent=bill_calculator.DEFAULT_DUTY_PERCENT,
            rebate_percent=0,
            surcharge_percent=0,
            other_charges=bill_calculator.DEFAULT_OTHER_CHARGES,
        )

        self.assertEqual(result.meter_rent, Decimal("20.00"))
        self.assertEqual(result.electricity_duty, Decimal("1.00"))
        self.assertEqual(result.other_charges, Decimal("100.00"))
        self.assertEqual(result.total, Decimal("141.00"))

    def test_bill_and_prompt_payment_rebate(self):
        result = bill_calculator.calculate_bill_amount(
            previous_reading=100,
            current_reading=200,
            sanctioned_load="1.2",
            meter_multiplier=1,
            meter_rent=10,
            duty_percent=5,
            rebate_percent=2,
            surcharge_percent=10,
            due_date=date(2026, 12, 31),
            payment_date=date(2026, 12, 31),
        )
        self.assertEqual(result.units, Decimal("100.00"))
        self.assertEqual(result.electricity_duty, Decimal("21.00"))
        self.assertEqual(result.prompt_payment_rebate, Decimal("9.02"))
        self.assertEqual(result.delayed_payment_surcharge, Decimal("0.00"))
        self.assertEqual(result.total, Decimal("441.98"))

    def test_late_payment_applies_surcharge_not_rebate(self):
        result = bill_calculator.calculate_bill_amount(
            previous_reading=100,
            current_reading=200,
            sanctioned_load=1,
            meter_multiplier=1,
            meter_rent=0,
            duty_percent=0,
            rebate_percent=2,
            surcharge_percent=10,
            due_date=date(2026, 12, 30),
            payment_date=date(2026, 12, 31),
        )
        self.assertEqual(result.prompt_payment_rebate, Decimal("0.00"))
        self.assertEqual(result.delayed_payment_surcharge, Decimal("40.00"))
        self.assertEqual(result.total, Decimal("440.00"))

    def test_reading_cannot_decrease(self):
        with self.assertRaisesRegex(ValueError, "cannot be less"):
            bill_calculator.calculate_bill_amount(
                previous_reading=200,
                current_reading=100,
                sanctioned_load=1,
                meter_multiplier=1,
                meter_rent=0,
                duty_percent=0,
                rebate_percent=0,
                surcharge_percent=0,
            )

    def test_pdf_export_creates_a_pdf_file(self):
        bill = (
            1, "2026-11-21", "2026-12-21", None, "12345", "Sample Consumer",
            "TPCODL", "Domestic General", 2.0, "Single-phase meter", 100.0,
            200.0, 100.0, 380.0, 40.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 420.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bill.pdf"
            bill_calculator.BillCalculatorApp._write_pdf(path, bill)
            self.assertTrue(path.read_bytes().startswith(b"%PDF-"))

    def test_sqlite_persists_consumer_and_bill_history(self):
        original_database_path = bill_calculator.DATABASE_PATH
        app = object.__new__(bill_calculator.BillCalculatorApp)
        with tempfile.TemporaryDirectory() as directory:
            bill_calculator.DATABASE_PATH = Path(directory) / "test.sqlite3"
            try:
                app._initialize_database()
                with app._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO consumers VALUES
                        ('12345', 'Sample Consumer', 'TPCODL',
                         'Domestic General', 2.0, 'Single-phase meter', 1.0)
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO consumer_accounts (consumer_number, password_hash)
                        VALUES ('12345', ?)
                        """,
                        (bill_calculator._hash_password("consumer-pass-123"),),
                    )
                    connection.execute(
                        """
                        INSERT INTO bills (
                            consumer_number, billing_date, due_date,
                            previous_reading, current_reading, units, energy_charge,
                            fixed_charge, meter_rent, duty_percent, electricity_duty,
                            other_charges, rebate_percent, prompt_payment_rebate,
                            surcharge_percent, delayed_payment_surcharge, total
                        ) VALUES (
                            '12345', '2026-11-21', '2026-12-21',
                            100, 200, 100, 380, 40, 0, 0, 0, 0, 0, 0, 0, 0, 420
                        )
                        """
                    )
                    history = connection.execute(
                        """
                        SELECT billing_date, units, total FROM bills
                        WHERE consumer_number='12345'
                        """
                    ).fetchone()
                    account = connection.execute(
                        """
                        SELECT password_hash FROM consumer_accounts
                        WHERE consumer_number='12345'
                        """
                    ).fetchone()
                self.assertEqual(history, ("2026-11-21", 100.0, 420.0))
                self.assertIsNotNone(account)
            finally:
                bill_calculator.DATABASE_PATH = original_database_path


class PasswordAuthenticationTests(unittest.TestCase):
    def test_password_hashes_are_salted_and_verified(self):
        first_hash = bill_calculator._hash_password("correct horse battery")
        second_hash = bill_calculator._hash_password("correct horse battery")

        self.assertNotEqual(first_hash, second_hash)
        self.assertTrue(
            bill_calculator._verify_password("correct horse battery", first_hash)
        )
        self.assertFalse(bill_calculator._verify_password("wrong password", first_hash))
        self.assertFalse(bill_calculator._verify_password("password", "malformed"))


class ConsumerAccessTests(unittest.TestCase):
    def test_consumers_can_register_only_admin_created_ids(self):
        original_database_path = bill_calculator.DATABASE_PATH
        app = object.__new__(bill_calculator.BillCalculatorApp)
        with tempfile.TemporaryDirectory() as directory:
            bill_calculator.DATABASE_PATH = Path(directory) / "registration.sqlite3"
            try:
                app._initialize_database()
                with self.assertRaisesRegex(ValueError, "administrator"):
                    app._create_consumer_account(
                        "unknown-id", "unused-code", "consumer-pass-123"
                    )

                with app._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO consumers VALUES
                        ('12345', 'Sample Consumer', 'TPCODL',
                         'Domestic General', 2.0, 'Single-phase meter', 1.0)
                        """
                    )

                    registration_code = "one-time-code"
                    connection.execute(
                        """
                        INSERT INTO consumer_registration_codes (
                            consumer_number, code_hash, expires_at
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            "12345",
                            hashlib.sha256(registration_code.encode()).hexdigest(),
                            time.time() + 600,
                        ),
                    )

                with self.assertRaisesRegex(ValueError, "invalid or expired"):
                    app._create_consumer_account(
                        "12345", "wrong-code", "consumer-pass-123"
                    )
                app._create_consumer_account(
                    "12345", registration_code, "consumer-pass-123"
                )
                with app._connect() as connection:
                    stored_hash = connection.execute(
                        """
                        SELECT password_hash FROM consumer_accounts
                        WHERE consumer_number='12345'
                        """
                    ).fetchone()[0]
                self.assertTrue(
                    bill_calculator._verify_password(
                        "consumer-pass-123", stored_hash
                    )
                )
                with self.assertRaisesRegex(ValueError, "already has a login"):
                    app._create_consumer_account(
                        "12345", registration_code, "another-pass-123"
                    )
            finally:
                bill_calculator.DATABASE_PATH = original_database_path

    def test_reports_payments_and_bill_exports_are_limited_to_consumer_id(self):
        class MemoryTree:
            def __init__(self):
                self.rows = {}

            def get_children(self):
                return tuple(self.rows)

            def delete(self, item):
                del self.rows[item]

            def insert(self, _parent, _index, iid, values):
                self.rows[iid] = {"values": values}

        class MemoryButton:
            def configure(self, **_kwargs):
                pass

        class MemoryVariable:
            def set(self, _value):
                pass

        original_database_path = bill_calculator.DATABASE_PATH
        app = object.__new__(bill_calculator.BillCalculatorApp)
        with tempfile.TemporaryDirectory() as directory:
            bill_calculator.DATABASE_PATH = Path(directory) / "access.sqlite3"
            try:
                app._initialize_database()
                with app._connect() as connection:
                    for consumer_number, name in (
                        ("12345", "Consumer One"),
                        ("67890", "Consumer Two"),
                    ):
                        connection.execute(
                            """
                            INSERT INTO consumers VALUES
                            (?, ?, 'TPCODL', 'Domestic General', 2.0,
                             'Single-phase meter', 1.0)
                            """,
                            (consumer_number, name),
                        )
                        connection.execute(
                            """
                            INSERT INTO bills (
                                consumer_number, billing_date, due_date,
                                previous_reading, current_reading, units,
                                energy_charge, fixed_charge, meter_rent,
                                duty_percent, electricity_duty, other_charges,
                                rebate_percent, prompt_payment_rebate,
                                surcharge_percent, delayed_payment_surcharge, total
                            ) VALUES (?, '2026-11-21', '2026-12-21',
                                      100, 200, 100, 380, 40, 0, 0, 0, 0,
                                      0, 0, 0, 0, 420)
                            """,
                            (consumer_number,),
                        )

                app.role = "consumer"
                app.consumer_number = "12345"
                app.payment_api_token = None
                app.payment_api_error = "Payment API unavailable"
                app.history_tree = MemoryTree()
                app.payment_tree = MemoryTree()
                app.payment_pay_button = MemoryButton()
                app.payment_status = MemoryVariable()
                app.refresh_history()
                app.refresh_payments()

                self.assertEqual(tuple(app.history_tree.rows), ("1",))
                self.assertEqual(tuple(app.payment_tree.rows), ("1",))
                app.selected_bill_id = 1
                self.assertEqual(app._selected_bill_data()[4], "12345")
                app.selected_bill_id = 2
                with self.assertRaisesRegex(ValueError, "no longer exists"):
                    app._selected_bill_data()
            finally:
                bill_calculator.DATABASE_PATH = original_database_path


if __name__ == "__main__":
    unittest.main()
