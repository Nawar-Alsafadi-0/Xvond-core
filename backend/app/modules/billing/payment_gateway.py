from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from backend.app.core.config.settings import settings


class PaymentGatewayError(RuntimeError):
    pass


class PaddleGateway:
    """Provider adapter for global Self-Service checkout.

    Xvond owns subscription entitlement. Paddle owns payment collection and
    merchant-of-record checkout. Provider credentials never leave this module.
    """

    provider = "paddle"

    def __init__(self) -> None:
        self.environment = settings.PADDLE_ENVIRONMENT
        self.api_key = settings.PADDLE_API_KEY
        self.webhook_secret = settings.PADDLE_WEBHOOK_SECRET
        self.checkout_url = settings.PADDLE_CHECKOUT_URL
        self.price_map_json = settings.PADDLE_PRICE_MAP_JSON
        self.webhook_tolerance_seconds = settings.PADDLE_WEBHOOK_TOLERANCE_SECONDS

    @property
    def api_base(self) -> str:
        return (
            "https://api.paddle.com"
            if self.environment == "live"
            else "https://sandbox-api.paddle.com"
        )

    def configured(self) -> bool:
        return bool(
            settings.BILLING_PROVIDER == self.provider
            and self.api_key
            and self.webhook_secret
        )

    def _price_map(self) -> dict[str, str]:
        try:
            value = json.loads(self.price_map_json or "{}")
        except ValueError as exc:
            raise PaymentGatewayError("Paddle price mapping is invalid") from exc
        if not isinstance(value, dict):
            raise PaymentGatewayError("Paddle price mapping must be an object")
        result: dict[str, str] = {}
        for key, raw in value.items():
            price_id = str(raw or "").strip()
            if price_id:
                result[str(key).strip()] = price_id
        return result

    def price_id_for(self, *, plan_id: int, tier: str) -> str:
        mapping = self._price_map()
        for key in (str(plan_id), str(tier or "").strip().lower()):
            value = mapping.get(key)
            if value:
                return value
        raise PaymentGatewayError(
            "The selected Xvond plan is not mapped to an online billing price"
        )

    def create_checkout(
        self,
        *,
        company_id: int,
        service_subscription_id: int,
        plan_id: int,
        plan_tier: str,
        service_code: str,
    ) -> dict[str, Any]:
        if not self.configured():
            raise PaymentGatewayError("Online billing is not configured")

        price_id = self.price_id_for(plan_id=plan_id, tier=plan_tier)
        body: dict[str, Any] = {
            "items": [{"price_id": price_id, "quantity": 1}],
            "collection_mode": "automatic",
            "custom_data": {
                "xvond_company_id": company_id,
                "xvond_service_subscription_id": service_subscription_id,
                "xvond_plan_id": plan_id,
                "xvond_service_code": service_code,
            },
        }
        if self.checkout_url:
            body["checkout"] = {"url": self.checkout_url}

        try:
            response = httpx.post(
                f"{self.api_base}/transactions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Paddle-Version": "1",
                },
                json=body,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayError("Online checkout could not be created") from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise PaymentGatewayError("Online checkout returned an invalid response")

        transaction_id = str(data.get("id") or "").strip()
        checkout = data.get("checkout") if isinstance(data.get("checkout"), dict) else {}
        checkout_url = str(checkout.get("url") or "").strip()
        if not transaction_id or not checkout_url:
            raise PaymentGatewayError("Online checkout is missing its payment URL")

        return {
            "provider": self.provider,
            "transaction_id": transaction_id,
            "subscription_id": str(data.get("subscription_id") or "").strip() or None,
            "status": str(data.get("status") or "pending").strip().lower(),
            "checkout_url": checkout_url,
        }

    def verify_webhook(self, *, raw_body: bytes, signature_header: str | None) -> None:
        if not self.webhook_secret:
            raise PaymentGatewayError("Payment webhook secret is not configured")
        values: dict[str, list[str]] = {}
        for part in str(signature_header or "").split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            values.setdefault(key.strip(), []).append(value.strip())

        timestamp = (values.get("ts") or [""])[0]
        signatures = [item for item in values.get("h1", []) if item]
        if not timestamp or not signatures:
            raise PaymentGatewayError("Payment webhook signature is missing")

        try:
            timestamp_int = int(timestamp)
        except ValueError as exc:
            raise PaymentGatewayError("Payment webhook timestamp is invalid") from exc

        if abs(int(time.time()) - timestamp_int) > self.webhook_tolerance_seconds:
            raise PaymentGatewayError("Payment webhook timestamp is outside tolerance")

        try:
            raw_text = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PaymentGatewayError("Payment webhook body is invalid") from exc

        signed_payload = f"{timestamp}:{raw_text}".encode("utf-8")
        expected = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise PaymentGatewayError("Payment webhook signature is invalid")


paddle_gateway = PaddleGateway()


def payment_gateway():
    if settings.BILLING_PROVIDER == "paddle":
        return paddle_gateway
    return None
