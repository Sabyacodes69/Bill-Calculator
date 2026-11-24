import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import app as payment_api
from backend.database import connection, initialize_database
from backend.security import hash_password


class PaymentApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = os.environ.get("BILL_CALCULATOR_DB_PATH")
        os.environ["BILL_CALCULATOR_DB_PATH"] = str(
            Path(self.temporary_directory.name) / "payment-test.sqlite3"
        )
        initialize_database()
        with connection() as db:
            db.execute(
                """
                INSERT INTO consumers VALUES
                ('12345', 'Consumer One', 'TPCODL', 'Domestic General',
                 2.0, 'Single-phase meter', 1.0)
                """
            )
            db.execute(
                """
                INSERT INTO consumers VALUES
                ('67890', 'Consumer Two', 'TPNODL', 'Domestic General',
                 2.0, 'Single-phase meter', 1.0)
                """
            )
            db.execute(
                "INSERT INTO admin_accounts VALUES (?, ?)",
                ("admin", hash_password("admin-password")),
            )
            db.execute(
                "INSERT INTO consumer_accounts VALUES (?, ?)",
                ("12345", hash_password("consumer-password")),
            )
            db.execute(
                "INSERT INTO consumer_accounts VALUES (?, ?)",
                ("67890", hash_password("second-password")),
            )
            for consumer_number in ("12345", "67890"):
                db.execute(
                    """
                    INSERT INTO bills (
                        consumer_number, billing_date, due_date,
                        previous_reading, current_reading, units, energy_charge,
                        fixed_charge, meter_rent, duty_percent, electricity_duty,
                        other_charges, rebate_percent, prompt_payment_rebate,
                        surcharge_percent, delayed_payment_surcharge, total
                    ) VALUES (?, '2026-11-24', '2026-12-24',
                              0, 100, 100, 380, 40, 20, 5, 21, 100,
                              0, 0, 0, 0, 123.45)
                    """,
                    (consumer_number,),
                )
        self.client_context = TestClient(payment_api.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        if self.original_database_path is None:
            os.environ.pop("BILL_CALCULATOR_DB_PATH", None)
        else:
            os.environ["BILL_CALCULATOR_DB_PATH"] = self.original_database_path
        self.temporary_directory.cleanup()

    def _login(self, role="consumer", username="12345", password="consumer-password"):
        response = self.client.post(
            "/api/auth/login",
            json={"role": role, "username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def test_role_checks_restrict_consumer_order_access_to_own_bill(self):
        token = self._login()
        response = self.client.post(
            "/api/payments/orders",
            json={"bill_id": 2},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 404)

        admin_token = self._login(
            role="admin", username="admin", password="admin-password"
        )
        response = self.client.get(
            "/api/payments/due",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_order_uses_server_calculated_due_and_status_clears_it(self):
        token = self._login()
        headers = {"Authorization": f"Bearer {token}"}
        due_response = self.client.get("/api/payments/due", headers=headers)
        self.assertEqual(due_response.status_code, 200)
        self.assertEqual(
            due_response.json()["bills"][0]["due_amount_paise"], 12345
        )

        with (
            patch.object(payment_api.phonepe, "is_configured", return_value=True),
            patch.object(
                payment_api.phonepe,
                "create_order",
                return_value={
                    "orderId": "phonepe-order-1",
                    "redirectUrl": "https://mercury.phonepe.com/checkout",
                    "state": "PENDING",
                },
            ) as create_order,
        ):
            order_response = self.client.post(
                "/api/payments/orders",
                json={"bill_id": 1},
                headers=headers,
            )
        self.assertEqual(order_response.status_code, 201, order_response.text)
        order = order_response.json()
        self.assertEqual(order["amount_paise"], 12345)
        create_order.assert_called_once_with(order["merchant_order_id"], 12345)

        with patch.object(
            payment_api.phonepe,
            "get_order_status",
            return_value={
                "orderId": "unrelated-order",
                "merchantOrderId": order["merchant_order_id"],
                "state": "COMPLETED",
                "amount": 12345,
            },
        ):
            mismatched_status = self.client.get(
                f"/api/payments/orders/{order['merchant_order_id']}",
                headers=headers,
            )
        self.assertEqual(mismatched_status.status_code, 502)

        with patch.object(
            payment_api.phonepe,
            "get_order_status",
            return_value={
                "orderId": "phonepe-order-1",
                "merchantOrderId": order["merchant_order_id"],
                "state": "COMPLETED",
                "amount": 12345,
            },
        ):
            status_response = self.client.get(
                f"/api/payments/orders/{order['merchant_order_id']}",
                headers=headers,
            )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["state"], "COMPLETED")
        self.assertEqual(
            self.client.get("/api/payments/due", headers=headers).json()["bills"],
            [],
        )

    def test_webhook_requires_sha_auth_and_is_idempotent(self):
        token = self._login()
        with (
            patch.object(payment_api.phonepe, "is_configured", return_value=True),
            patch.object(
                payment_api.phonepe,
                "create_order",
                return_value={
                    "orderId": "phonepe-order-2",
                    "redirectUrl": "https://mercury.phonepe.com/checkout",
                    "state": "PENDING",
                },
            ),
        ):
            order_response = self.client.post(
                "/api/payments/orders",
                json={"bill_id": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
        order_id = order_response.json()["merchant_order_id"]
        event = {
            "event": "checkout.order.completed",
            "payload": {
                "merchantOrderId": order_id,
                "orderId": "phonepe-order-2",
                "state": "COMPLETED",
                "amount": 12345,
            },
        }
        with patch.dict(
            os.environ,
            {
                "PHONEPE_WEBHOOK_USERNAME": "hook-user",
                "PHONEPE_WEBHOOK_PASSWORD": "hook-pass",
            },
        ):
            invalid = self.client.post(
                "/api/payments/phonepe/webhook",
                content=json.dumps(event),
                headers={"Authorization": "wrong"},
            )
            self.assertEqual(invalid.status_code, 401)
            authorization = hashlib.sha256(
                b"hook-user:hook-pass"
            ).hexdigest()
            invalid_order = {
                **event,
                "payload": {**event["payload"], "orderId": "unrelated-order"},
            }
            rejected_order = self.client.post(
                "/api/payments/phonepe/webhook",
                content=json.dumps(invalid_order),
                headers={"Authorization": authorization},
            )
            accepted = self.client.post(
                "/api/payments/phonepe/webhook",
                content=json.dumps(event),
                headers={"Authorization": authorization},
            )
            repeated = self.client.post(
                "/api/payments/phonepe/webhook",
                content=json.dumps(event),
                headers={"Authorization": authorization},
            )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(rejected_order.status_code, 400)
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(
            self.client.get(
                "/api/payments/due",
                headers={"Authorization": f"Bearer {token}"},
            ).json()["bills"],
            [],
        )

    def test_consumer_registration_requires_admin_created_id(self):
        response = self.client.post(
            "/api/auth/register",
            json={
                "consumer_number": "unknown",
                "registration_code": "invalid",
                "password": "new-password",
            },
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            "/api/auth/register",
            json={
                "consumer_number": "12345",
                "registration_code": "unused",
                "password": "new-password",
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_registration_code_is_issued_by_admin_and_consumed_once(self):
        admin_token = self._login(
            role="admin", username="admin", password="admin-password"
        )
        response = self.client.post(
            "/api/admin/consumers",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "consumer_number": "55555",
                "name": "New Consumer",
                "discom": "TPCODL",
                "category": "Domestic General",
                "sanctioned_load": 2,
                "meter_type": "Single-phase meter",
                "meter_multiplier": 1,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        registration_code = response.json()["registration_code"]

        invalid = self.client.post(
            "/api/auth/register",
            json={
                "consumer_number": "55555",
                "registration_code": "not-the-code",
                "password": "new-consumer-pass",
            },
        )
        self.assertEqual(invalid.status_code, 401)
        registered = self.client.post(
            "/api/auth/register",
            json={
                "consumer_number": "55555",
                "registration_code": registration_code,
                "password": "new-consumer-pass",
            },
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        replayed = self.client.post(
            "/api/auth/register",
            json={
                "consumer_number": "55555",
                "registration_code": registration_code,
                "password": "another-consumer-pass",
            },
        )
        self.assertEqual(replayed.status_code, 409)


if __name__ == "__main__":
    unittest.main()
