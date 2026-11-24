# Electricity bill payment backend

This FastAPI service is the trusted server-side boundary for consumer sessions,
bill ownership checks, due calculations, and PhonePe Standard Checkout. PhonePe
client credentials and webhook credentials are read only from the backend
environment. They must never be copied into the Tkinter app or returned by an
API response.

## Local setup

Install the project dependencies and start the API from the project directory:

```powershell
python -m pip install -r requirements.txt
$env:BILL_CALCULATOR_API_URL = "http://127.0.0.1:8000"
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

By default, the service opens the existing `bill_calculator.sqlite3` beside the
desktop app. Override this with `BILL_CALCULATOR_DB_PATH` if the API and app use
another shared database file. The API initializes its session/payment tables
without changing existing consumers or bills. When an administrator creates a
consumer, the app/API displays a one-time registration code that must be given
to that consumer; it expires after seven days and is required to set the
consumer's password. The code is stored only as a hash and cannot be retrieved
later.

For PhonePe sandbox checkout, configure these variables in the backend process:

```text
PHONEPE_ENV=sandbox
PHONEPE_CLIENT_ID=<from PhonePe Business Developer Settings>
PHONEPE_CLIENT_SECRET=<from PhonePe Business Developer Settings>
PHONEPE_CLIENT_VERSION=<from PhonePe Business Developer Settings>
PHONEPE_REDIRECT_URL=https://your-domain.example/payment-return
PHONEPE_WEBHOOK_USERNAME=<webhook username configured in PhonePe>
PHONEPE_WEBHOOK_PASSWORD=<webhook password configured in PhonePe>
```

Do not put real values in source control, the desktop app, or API requests from
the client. Use a secret manager or protected environment configuration on the
server. Use `PHONEPE_ENV=production` only after successful UAT and approval from
PhonePe.

## Production deployment

PhonePe webhooks require a publicly reachable HTTPS URL. Run Uvicorn behind a
TLS-terminating reverse proxy and point the PhonePe webhook to
`https://<your-host>/api/payments/phonepe/webhook`. Configure the webhook in
PhonePe Developer Settings using SHA authentication with the same webhook
username and password set on the server. Subscribe to
`checkout.order.completed` and `checkout.order.failed`.

The included SQLite adapter is appropriate for development or a single-host,
low-volume deployment. Keep the database on persistent storage and back it up.
For multiple application instances or higher payment volume, migrate the adapter
to PostgreSQL before scaling; do not put a SQLite file on a network share.
The Tkinter app and API must use the same authoritative database for bill IDs
and consumer accounts. A production multi-user rollout should make the API
database authoritative for consumer/bill writes as well, rather than relying on
independent desktop-local copies.

## API endpoints

Interactive OpenAPI documentation is available at `/docs`; the schema is at
`/openapi.json`.

All authenticated endpoints use `Authorization: Bearer <access_token>`. Tokens
are opaque, stored hashed in the database, and expire after eight hours.

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| `GET` | `/health` | Public | Liveness check |
| `POST` | `/api/auth/login` | Public | Authenticate an `admin` or `consumer` |
| `POST` | `/api/auth/register` | Public | Register with an admin-created consumer ID and one-time code |
| `GET` | `/api/auth/me` | Authenticated | Return the token's role and identity |
| `POST` | `/api/auth/logout` | Authenticated | Revoke the current bearer token |
| `POST` | `/api/admin/consumers` | Admin | Create a consumer ID/profile and issue its one-time registration code |
| `GET` | `/api/payments/due` | Consumer | List that consumer's bills with remaining due in paise |
| `POST` | `/api/payments/orders` | Consumer | Create/reuse a PhonePe order for one bill's server-calculated balance |
| `GET` | `/api/payments/orders/{merchant_order_id}` | Owning consumer | Poll PhonePe for verified order status |
| `POST` | `/api/payments/phonepe/webhook` | PhonePe SHA auth | Receive idempotent order-completed/failed events |

### Example requests

Consumer login:

```json
{
  "role": "consumer",
  "username": "CONSUMER-ID",
  "password": "consumer password"
}
```

Consumer self-registration requires the one-time code supplied by the
administrator:

```json
{
  "consumer_number": "CONSUMER-ID",
  "registration_code": "one-time code",
  "password": "consumer password"
}
```

The code expires after seven days and is consumed on successful registration.
Issue a new code through the admin desktop app by saving the consumer profile
again before the consumer registers.

Create a payment order (the client supplies only the bill ID, never an amount):

```json
{
  "bill_id": 42
}
```

The response contains the PhonePe `redirect_url`, merchant order ID, state, and
amount in paise. Open the redirect URL in the user's browser; then poll the
status endpoint. The API marks an order completed only when PhonePe's status
reports `COMPLETED` and its amount matches the stored order. Completed orders
reduce that bill's active due. The PhonePe webhook is an additional real-time
update path; the status API remains the fallback.

## PhonePe references

- [Website Standard Checkout flow](https://developer.phonepe.com/payment-gateway/website-integration/standard-checkout/api-integration/integration-steps)
- [Authorization token](https://developer.phonepe.com/payment-gateway/website-integration/standard-checkout/api-integration/api-reference/authorization)
- [Create Payment](https://developer.phonepe.com/payment-gateway/website-integration/standard-checkout/api-integration/api-reference/create-payment)
- [Order Status](https://developer.phonepe.com/payment-gateway/website-integration/standard-checkout/api-integration/api-reference/order-status)
- [Webhook handling](https://developer.phonepe.com/payment-gateway/website-integration/standard-checkout/api-integration/api-reference/webhook)
