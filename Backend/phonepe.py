"""Server-only PhonePe Standard Checkout integration."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class PhonePeError(RuntimeError):
    """A safe-to-display PhonePe integration error."""


class PhonePeClient:
    def __init__(self) -> None:
        self._access_token: str | None = None
        self._access_token_expires_at = 0
        self._token_lock = threading.Lock()

    @staticmethod
    def is_configured() -> bool:
        return all(
            os.environ.get(key)
            for key in (
                "PHONEPE_CLIENT_ID",
                "PHONEPE_CLIENT_SECRET",
                "PHONEPE_CLIENT_VERSION",
                "PHONEPE_REDIRECT_URL",
            )
        )

    @staticmethod
    def _environment() -> tuple[str, str]:
        mode = os.environ.get("PHONEPE_ENV", "sandbox").strip().lower()
        if mode == "sandbox":
            return (
                "https://api-preprod.phonepe.com/apis/pg-sandbox",
                "https://api-preprod.phonepe.com/apis/pg-sandbox/v1/oauth/token",
            )
        if mode == "production":
            return (
                "https://api.phonepe.com/apis/pg",
                "https://api.phonepe.com/apis/identity-manager/v1/oauth/token",
            )
        raise PhonePeError("PHONEPE_ENV must be 'sandbox' or 'production'.")

    @staticmethod
    def _request_json(request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read()
        except HTTPError as error:
            raise PhonePeError(
                f"PhonePe returned HTTP {error.code}."
            ) from error
        except URLError as error:
            raise PhonePeError("Could not reach PhonePe.") from error
        try:
            result = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise PhonePeError("PhonePe returned an invalid response.") from error
        if not isinstance(result, dict):
            raise PhonePeError("PhonePe returned an invalid response.")
        return result

    def _get_access_token(self) -> str:
        with self._token_lock:
            if (
                self._access_token
                and self._access_token_expires_at > time.time() + 60
            ):
                return self._access_token

            if not self.is_configured():
                raise PhonePeError("PhonePe credentials are not configured.")
            _, token_url = self._environment()
            form = urlencode(
                {
                    "client_id": os.environ["PHONEPE_CLIENT_ID"],
                    "client_version": os.environ["PHONEPE_CLIENT_VERSION"],
                    "client_secret": os.environ["PHONEPE_CLIENT_SECRET"],
                    "grant_type": "client_credentials",
                }
            ).encode("ascii")
            request = Request(
                token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            response = self._request_json(request)
            token = response.get("access_token")
            if not isinstance(token, str) or not token:
                raise PhonePeError("PhonePe did not return an access token.")
            expires_at = response.get("expires_at")
            try:
                expiration = int(expires_at)
            except (TypeError, ValueError):
                expiration = int(time.time()) + 3000
            self._access_token = token
            self._access_token_expires_at = expiration
            return token

    def create_order(self, merchant_order_id: str, amount_paise: int) -> dict[str, Any]:
        if not self.is_configured():
            raise PhonePeError("PhonePe credentials or redirect URL are not configured.")
        api_base, _ = self._environment()
        redirect_url = os.environ["PHONEPE_REDIRECT_URL"]
        parsed_redirect = urlsplit(redirect_url)
        if parsed_redirect.scheme != "https" or not parsed_redirect.netloc:
            raise PhonePeError("PHONEPE_REDIRECT_URL must be an HTTPS URL.")

        payload = {
            "merchantOrderId": merchant_order_id,
            "amount": amount_paise,
            "expireAfter": 1800,
            "paymentFlow": {
                "type": "PG_CHECKOUT",
                "message": "Electricity bill payment",
                "merchantUrls": {"redirectUrl": redirect_url},
                "paymentModeConfig": {
                    "enabledPaymentModes": [
                        {"type": "UPI_INTENT"},
                        {"type": "UPI_QR"},
                    ]
                },
            },
        }
        request = Request(
            f"{api_base}/checkout/v2/pay",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"O-Bearer {self._get_access_token()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        response = self._request_json(request)
        redirect = response.get("redirectUrl")
        redirect_host = urlsplit(redirect).hostname if isinstance(redirect, str) else None
        if (
            response.get("state") != "PENDING"
            or not isinstance(response.get("orderId"), str)
            or not redirect_host
            or not (redirect_host == "phonepe.com" or redirect_host.endswith(".phonepe.com"))
            or urlsplit(redirect).scheme != "https"
        ):
            raise PhonePeError("PhonePe did not return a valid checkout session.")
        return response

    def get_order_status(self, merchant_order_id: str) -> dict[str, Any]:
        if not self.is_configured():
            raise PhonePeError("PhonePe credentials are not configured.")
        api_base, _ = self._environment()
        request = Request(
            f"{api_base}/checkout/v2/order/{merchant_order_id}/status?details=false",
            headers={
                "Authorization": f"O-Bearer {self._get_access_token()}",
                "Content-Type": "application/json",
            },
            method="GET",
        )
        return self._request_json(request)
