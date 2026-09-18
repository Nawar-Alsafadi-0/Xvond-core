from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

from backend.app.core.config.settings import settings


class PaymentGatewayError(RuntimeError):
    pass


class PaymentGatewayOutcomeUnknown(PaymentGatewayError):
    """The provider may have accepted a side effect but Xvond cannot prove it."""


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
        amount: Decimal,
        currency: str,
        customer_email: str | None = None,
        customer_name: str | None = None,
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


class TapGateway:
    """Tap Payments hosted-charge adapter for Xvond Self-Service checkout."""

    provider = "tap"
    api_base = "https://api.tap.company/v2"

    def configured(self) -> bool:
        return bool(
            settings.BILLING_PROVIDER == self.provider
            and settings.TAP_SECRET_KEY
            and settings.TAP_MERCHANT_ID
            and settings.PUBLIC_BASE_URL
        )

    @staticmethod
    def _customer_name(value: str | None) -> tuple[str, str]:
        clean = " ".join(str(value or "").strip().split())
        if not clean:
            return "Xvond", "Customer"
        parts = clean.split(" ", 1)
        return parts[0][:80], (parts[1] if len(parts) > 1 else "Customer")[:80]

    def create_checkout(
        self,
        *,
        company_id: int,
        service_subscription_id: int,
        plan_id: int,
        plan_tier: str,
        service_code: str,
        amount: Decimal,
        currency: str,
        customer_email: str | None = None,
        customer_name: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured():
            raise PaymentGatewayError("Online billing is not configured")

        first_name, last_name = self._customer_name(customer_name)
        email = str(customer_email or "").strip()
        if not email:
            raise PaymentGatewayError("Tap checkout requires a customer email")

        redirect_url = settings.TAP_REDIRECT_URL or (
            f"{settings.PUBLIC_BASE_URL}/billing/return"
        )
        webhook_url = f"{settings.PUBLIC_BASE_URL}/webhooks/billing/tap"
        idempotency = f"xvond-sub-{service_subscription_id}-plan-{plan_id}"

        body = {
            "amount": float(Decimal(str(amount))),
            "currency": str(currency or "").upper(),
            "customer_initiated": True,
            "threeDSecure": True,
            "save_card": bool(settings.TAP_SAVE_CARD_FOR_RECURRING),
            "description": f"Xvond AI Employee - {plan_tier}",
            "metadata": {
                "xvond_company_id": str(company_id),
                "xvond_service_subscription_id": str(service_subscription_id),
                "xvond_plan_id": str(plan_id),
                "xvond_service_code": str(service_code),
            },
            "reference": {
                "transaction": f"xvond-{service_subscription_id}-{plan_id}",
                "order": f"xvond-{company_id}-{service_subscription_id}",
                "idempotent": idempotency,
            },
            "customer": {
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
            },
            "merchant": {"id": settings.TAP_MERCHANT_ID},
            "source": {"id": settings.TAP_SOURCE_ID},
            "post": {"url": webhook_url},
            "redirect": {"url": redirect_url},
        }

        try:
            response = httpx.post(
                f"{self.api_base}/charges/",
                headers={
                    "Authorization": f"Bearer {settings.TAP_SECRET_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayError("Tap checkout could not be created") from exc

        if not isinstance(payload, dict):
            raise PaymentGatewayError("Tap checkout returned an invalid response")

        charge_id = str(payload.get("id") or "").strip()
        status = str(payload.get("status") or "").strip().lower()
        transaction = payload.get("transaction")
        transaction = transaction if isinstance(transaction, dict) else {}
        checkout_url = str(transaction.get("url") or "").strip()
        if not charge_id:
            raise PaymentGatewayError("Tap checkout is missing its charge id")
        if status != "captured" and not checkout_url:
            raise PaymentGatewayError("Tap checkout is missing its payment URL")

        return {
            "provider": self.provider,
            "transaction_id": charge_id,
            "subscription_id": None,
            "status": "completed" if status == "captured" else "pending",
            "checkout_url": checkout_url or redirect_url,
        }

    @staticmethod
    def _amount_for_hash(value: Any, currency: str) -> str:
        decimals = 3 if str(currency or "").upper() in {"BHD", "JOD", "KWD", "OMR"} else 2
        quantum = Decimal("0.001") if decimals == 3 else Decimal("0.01")
        amount = Decimal(str(value or "0")).quantize(quantum, rounding=ROUND_HALF_UP)
        return f"{amount:.{decimals}f}"

    def create_saved_card_token(
        self,
        *,
        customer_id: str,
        card_id: str,
    ) -> str:
        if not self.configured():
            raise PaymentGatewayError("Tap billing is not configured")
        try:
            response = httpx.post(
                f"{self.api_base}/tokens/",
                headers={
                    "Authorization": f"Bearer {settings.TAP_SECRET_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "saved_card": {
                        "card_id": str(card_id),
                        "customer_id": str(customer_id),
                    },
                    "client_ip": "127.0.0.1",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayError("Tap saved-card token could not be created") from exc

        token_id = str(payload.get("id") or "").strip() if isinstance(payload, dict) else ""
        if not token_id:
            raise PaymentGatewayError("Tap saved-card token response is missing token id")
        return token_id

    def create_recurring_charge(
        self,
        *,
        company_id: int,
        service_subscription_id: int,
        plan_id: int,
        plan_tier: str,
        service_code: str,
        amount: Decimal,
        currency: str,
        customer_id: str,
        card_id: str,
        payment_agreement_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        token_id = self.create_saved_card_token(
            customer_id=customer_id,
            card_id=card_id,
        )
        webhook_url = f"{settings.PUBLIC_BASE_URL}/webhooks/billing/tap"
        body = {
            "amount": float(Decimal(str(amount))),
            "currency": str(currency or "").upper(),
            "customer_initiated": False,
            "threeDSecure": False,
            "save_card": False,
            "payment_agreement": {"id": str(payment_agreement_id)},
            "description": f"Xvond AI Employee renewal - {plan_tier}",
            "metadata": {
                "xvond_company_id": str(company_id),
                "xvond_service_subscription_id": str(service_subscription_id),
                "xvond_plan_id": str(plan_id),
                "xvond_service_code": str(service_code),
                "xvond_renewal": "true",
                "xvond_renewal_key": str(idempotency_key),
            },
            "reference": {
                "transaction": f"xvond-renew-{service_subscription_id}-{plan_id}",
                "order": f"xvond-{company_id}-{service_subscription_id}",
                "idempotent": str(idempotency_key),
            },
            "customer": {"id": str(customer_id)},
            "merchant": {"id": settings.TAP_MERCHANT_ID},
            "source": {"id": token_id},
            "post": {"url": webhook_url},
        }

        try:
            response = httpx.post(
                f"{self.api_base}/charges/",
                headers={
                    "Authorization": f"Bearer {settings.TAP_SECRET_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentGatewayOutcomeUnknown(
                "Tap recurring charge outcome is unknown"
            ) from exc

        if not isinstance(payload, dict):
            raise PaymentGatewayError("Tap recurring charge returned an invalid response")
        charge_id = str(payload.get("id") or "").strip()
        status = str(payload.get("status") or "").strip().lower()
        if not charge_id:
            raise PaymentGatewayError("Tap recurring charge is missing its charge id")
        return {
            "provider": self.provider,
            "transaction_id": charge_id,
            "status": status or "pending",
        }

    def verify_webhook(
        self,
        *,
        payload: dict[str, Any],
        hashstring_header: str | None,
    ) -> None:
        secret = settings.TAP_SECRET_KEY
        posted = str(hashstring_header or "").strip().lower()
        if not secret or not posted:
            raise PaymentGatewayError("Tap webhook hashstring is missing")

        reference = payload.get("reference")
        reference = reference if isinstance(reference, dict) else {}
        transaction = payload.get("transaction")
        transaction = transaction if isinstance(transaction, dict) else {}

        charge_id = str(payload.get("id") or "")
        currency = str(payload.get("currency") or "").upper()
        amount = self._amount_for_hash(payload.get("amount"), currency)
        gateway_reference = str(reference.get("gateway") or "")
        payment_reference = str(reference.get("payment") or "")
        status = str(payload.get("status") or "")
        created = str(transaction.get("created") or payload.get("created") or "")

        to_hash = (
            f"x_id{charge_id}"
            f"x_amount{amount}"
            f"x_currency{currency}"
            f"x_gateway_reference{gateway_reference}"
            f"x_payment_reference{payment_reference}"
            f"x_status{status}"
            f"x_created{created}"
        )
        expected = hmac.new(
            secret.encode("utf-8"),
            to_hash.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected.lower(), posted):
            raise PaymentGatewayError("Tap webhook hashstring is invalid")


paddle_gateway = PaddleGateway()
tap_gateway = TapGateway()


def payment_gateway():
    if settings.BILLING_PROVIDER == "paddle":
        return paddle_gateway
    if settings.BILLING_PROVIDER == "tap":
        return tap_gateway
    return None
