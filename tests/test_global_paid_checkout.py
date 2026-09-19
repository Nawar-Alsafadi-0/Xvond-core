import hashlib
import hmac
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.main import app
from backend.app.api import billing_webhooks
from backend.app.api import customer_subscription as subscription_api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.billing.payment_gateway import PaddleGateway, TapGateway
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentEvent,
    ServicePlan,
    ServiceSubscription,
)


OWNER = SimpleNamespace(
    company_id=1,
    role="owner",
    id=10,
    email="owner@example.test",
    full_name="Nawar Owner",
)


class FakePaddleResponse:
    def __init__(self, payload, status_code=201):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://sandbox-api.paddle.com/transactions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self):
        return self.payload


class FakeGateway:
    provider = "paddle"

    def configured(self):
        return True

    def create_checkout(self, **kwargs):
        return {
            "provider": "paddle",
            "transaction_id": "txn_test_123",
            "subscription_id": None,
            "status": "draft",
            "checkout_url": "https://checkout.example.test/pay?_ptxn=txn_test_123",
        }


@pytest.fixture
def payment_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(subscription_api, "SessionLocal", factory)
    monkeypatch.setattr(billing_webhooks, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            )
        )
        db.flush()
        db.add(
            ServicePlan(
                id=2,
                service_code="ai_agents",
                tier="business",
                name="Business",
                monthly_price=Decimal("19.900"),
                currency="USD",
                limits={"agents": 1, "channels": 2},
                enabled=True,
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_paddle_gateway_creates_server_side_transaction(monkeypatch):
    gateway = PaddleGateway()
    gateway.environment = "sandbox"
    gateway.api_key = "pdl_sdbx_test"
    gateway.webhook_secret = "whsec"
    gateway.checkout_url = "https://xvond.example/checkout"
    gateway.price_map_json = '{"business":"pri_test_business"}'
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.BILLING_PROVIDER",
        "paddle",
    )

    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakePaddleResponse(
            {
                "data": {
                    "id": "txn_01test",
                    "status": "draft",
                    "subscription_id": None,
                    "checkout": {
                        "url": "https://xvond.example/checkout?_ptxn=txn_01test"
                    },
                }
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = gateway.create_checkout(
        company_id=7,
        service_subscription_id=8,
        plan_id=2,
        plan_tier="business",
        service_code="ai_agents",
        amount=Decimal("19.900"),
        currency="USD",
        customer_email="owner@example.test",
        customer_name="Nawar Owner",
    )

    assert captured["url"] == "https://sandbox-api.paddle.com/transactions"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert captured["headers"]["Paddle-Version"] == "1"
    assert captured["json"]["items"] == [
        {"price_id": "pri_test_business", "quantity": 1}
    ]
    assert captured["json"]["custom_data"]["xvond_company_id"] == 7
    assert captured["json"]["custom_data"]["xvond_service_subscription_id"] == 8
    assert result["transaction_id"] == "txn_01test"
    assert "_ptxn=txn_01test" in result["checkout_url"]


def test_paddle_webhook_signature_uses_raw_body(monkeypatch):
    gateway = PaddleGateway()
    gateway.webhook_secret = "top-secret"
    gateway.webhook_tolerance_seconds = 30
    raw = b'{"event_id":"evt_1","event_type":"transaction.completed"}'
    timestamp = 2000000000
    signature = hmac.new(
        gateway.webhook_secret.encode(),
        f"{timestamp}:{raw.decode()}".encode(),
        hashlib.sha256,
    ).hexdigest()
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.time.time",
        lambda: timestamp,
    )

    gateway.verify_webhook(
        raw_body=raw,
        signature_header=f"ts={timestamp};h1={signature}",
    )


def test_paid_plan_creates_checkout_but_not_entitlement(payment_database, monkeypatch):
    factory = payment_database
    monkeypatch.setattr(
        subscription_api,
        "_online_payment_gateway",
        lambda: FakeGateway(),
    )

    result = subscription_api.request_self_service_ai_agent_subscription(
        subscription_api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )

    assert result["status"] == "pending_payment"
    assert result["online_payment"] is True
    assert result["checkout"]["provider"] == "paddle"
    assert result["checkout"]["checkout_url"].startswith("https://checkout.example.test/")

    with factory() as db:
        subscription = db.query(ServiceSubscription).one()
        checkout = db.query(ServiceCheckout).one()
        assert subscription.status == "pending_payment"
        assert checkout.provider_transaction_id == "txn_test_123"
        with pytest.raises(Exception) as exc:
            service_limits.entitlement(db, 1, "ai_agents")
        assert getattr(exc.value, "status_code", None) == 403


def test_completed_payment_webhook_activates_once(payment_database, monkeypatch):
    factory = payment_database
    monkeypatch.setattr(
        subscription_api,
        "_online_payment_gateway",
        lambda: FakeGateway(),
    )
    created = subscription_api.request_self_service_ai_agent_subscription(
        subscription_api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )
    subscription_id = created["subscription"]["id"]

    monkeypatch.setattr(
        billing_webhooks.paddle_gateway,
        "verify_webhook",
        lambda **kwargs: None,
    )
    client = TestClient(app)
    payload = {
        "event_id": "evt_paid_1",
        "event_type": "transaction.completed",
        "data": {
            "id": "txn_test_123",
            "status": "completed",
            "subscription_id": "sub_test_123",
            "custom_data": {
                "xvond_company_id": 1,
                "xvond_service_subscription_id": subscription_id,
                "xvond_plan_id": 2,
                "xvond_service_code": "ai_agents",
            },
            "billing_period": {
                "starts_at": "2026-09-18T12:00:00Z",
                "ends_at": "2026-10-18T12:00:00Z",
            },
        },
    }

    first = client.post(
        "/webhooks/billing/paddle",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Paddle-Signature": "test",
        },
    )
    second = client.post(
        "/webhooks/billing/paddle",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Paddle-Signature": "test",
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "processed"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    with factory() as db:
        subscription = db.get(ServiceSubscription, subscription_id)
        checkout = db.query(ServiceCheckout).one()
        assert subscription.status == "active"
        assert str(subscription.current_period_start) == "2026-09-18 12:00:00"
        assert str(subscription.current_period_end) == "2026-10-18 12:00:00"
        assert checkout.status == "completed"
        assert checkout.provider_subscription_id == "sub_test_123"
        event = db.query(ServicePaymentEvent).one()
        assert event.company_id == 1
        assert event.service_checkout_id == checkout.id
        entitlement, plan = service_limits.entitlement(db, 1, "ai_agents")
        assert entitlement.id == subscription_id
        assert plan.id == 2


def test_public_billing_config_exposes_only_browser_safe_values(monkeypatch):
    from backend.app.api import public_billing

    monkeypatch.setattr(public_billing.settings, "BILLING_PROVIDER", "paddle")
    monkeypatch.setattr(public_billing.settings, "PADDLE_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(public_billing.settings, "PADDLE_CLIENT_TOKEN", "test_client_token")
    monkeypatch.setattr(public_billing.settings, "PADDLE_CHECKOUT_URL", "https://xvond.example/checkout")
    monkeypatch.setattr(public_billing.settings, "PADDLE_API_KEY", "secret-api-key")
    monkeypatch.setattr(public_billing.settings, "PADDLE_WEBHOOK_SECRET", "secret-webhook")
    monkeypatch.setattr(public_billing.settings, "PUBLIC_BASE_URL", "https://xvond.example")

    result = public_billing.public_billing_config()

    assert result == {
        "enabled": True,
        "provider": "paddle",
        "checkout_mode": "xvond_overlay",
        "environment": "sandbox",
        "client_token": "test_client_token",
        "success_url": "https://xvond.example/customer-ui#employee-builder",
    }
    assert "secret-api-key" not in str(result)
    assert "secret-webhook" not in str(result)


def test_public_billing_config_reports_tap_redirect_mode_without_secrets(monkeypatch):
    from backend.app.api import public_billing

    monkeypatch.setattr(public_billing.settings, "BILLING_PROVIDER", "tap")
    monkeypatch.setattr(public_billing.settings, "TAP_SECRET_KEY", "sk_test_hidden")
    monkeypatch.setattr(public_billing.settings, "TAP_MERCHANT_ID", "merchant_test")
    monkeypatch.setattr(public_billing.settings, "PUBLIC_BASE_URL", "https://xvond.example")

    result = public_billing.public_billing_config()

    assert result == {
        "enabled": True,
        "provider": "tap",
        "checkout_mode": "provider_redirect",
        "success_url": "https://xvond.example/customer-ui#employee-builder",
    }
    assert "sk_test_hidden" not in str(result)
    assert "merchant_test" not in str(result)


def test_xvond_checkout_page_uses_paddle_js_without_client_side_entitlement_logic():
    from pathlib import Path

    page = Path("frontend/public/checkout.html").read_text(encoding="utf-8")
    assert "https://cdn.paddle.com/paddle/v2/paddle.js" in page
    assert "Paddle.Initialize" in page
    assert "Paddle.Checkout.open" in page
    assert "transactionId" in page
    assert "checkout.completed" in page
    assert "/customer-ui#employee-builder" in page
    assert "PADDLE_API_KEY" not in page
    assert "PADDLE_WEBHOOK_SECRET" not in page


def test_tap_gateway_creates_hosted_charge_without_card_data(monkeypatch):
    gateway = TapGateway()
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.BILLING_PROVIDER",
        "tap",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_SECRET_KEY",
        "sk_test_tap_secret",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_MERCHANT_ID",
        "merchant_test",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_SOURCE_ID",
        "src_all",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_SAVE_CARD_FOR_RECURRING",
        True,
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.PUBLIC_BASE_URL",
        "https://api.xvond.example",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_REDIRECT_URL",
        "",
    )

    captured = {}

    def fake_post(url, *, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakePaddleResponse(
            {
                "id": "chg_test_123",
                "status": "INITIATED",
                "transaction": {
                    "url": "https://tap.example/pay/chg_test_123",
                    "created": "1760000000000",
                },
            },
            status_code=200,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = gateway.create_checkout(
        company_id=7,
        service_subscription_id=8,
        plan_id=2,
        plan_tier="business",
        service_code="ai_agents",
        amount=Decimal("19.900"),
        currency="OMR",
        customer_email="owner@example.test",
        customer_name="Nawar Owner",
    )

    assert captured["url"] == "https://api.tap.company/v2/charges/"
    assert captured["headers"]["Authorization"] == "Bearer sk_test_tap_secret"
    body = captured["json"]
    assert body["amount"] == 19.9
    assert body["currency"] == "OMR"
    assert body["source"]["id"] == "src_all"
    assert body["merchant"]["id"] == "merchant_test"
    assert body["save_card"] is True
    assert body["post"]["url"] == "https://api.xvond.example/webhooks/billing/tap"
    assert body["redirect"]["url"] == "https://api.xvond.example/billing/return"
    assert body["reference"]["idempotent"] == "xvond-sub-8-plan-2"
    assert body["metadata"]["xvond_company_id"] == "7"
    assert body["customer"]["email"] == "owner@example.test"
    assert "card" not in body
    assert result["provider"] == "tap"
    assert result["transaction_id"] == "chg_test_123"
    assert result["checkout_url"] == "https://tap.example/pay/chg_test_123"


def test_tap_webhook_hashstring_uses_currency_precision(monkeypatch):
    gateway = TapGateway()
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_SECRET_KEY",
        "sk_test_hash_secret",
    )
    payload = {
        "id": "chg_hash_1",
        "amount": 19.9,
        "currency": "OMR",
        "status": "CAPTURED",
        "reference": {
            "gateway": "gw_1",
            "payment": "pay_1",
        },
        "transaction": {"created": "1760000000000"},
    }
    to_hash = (
        "x_idchg_hash_1"
        "x_amount19.900"
        "x_currencyOMR"
        "x_gateway_referencegw_1"
        "x_payment_referencepay_1"
        "x_statusCAPTURED"
        "x_created1760000000000"
    )
    signature = hmac.new(
        b"sk_test_hash_secret",
        to_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    gateway.verify_webhook(
        payload=payload,
        hashstring_header=signature,
    )


def test_tap_captured_webhook_activates_subscription_once(payment_database, monkeypatch):
    factory = payment_database
    fake_gateway = SimpleNamespace(
        provider="tap",
        configured=lambda: True,
        create_checkout=lambda **kwargs: {
            "provider": "tap",
            "transaction_id": "chg_paid_123",
            "subscription_id": None,
            "status": "pending",
            "checkout_url": "https://tap.example/pay/chg_paid_123",
        },
    )
    monkeypatch.setattr(subscription_api, "_online_payment_gateway", lambda: fake_gateway)

    created = subscription_api.request_self_service_ai_agent_subscription(
        subscription_api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )
    subscription_id = created["subscription"]["id"]

    monkeypatch.setattr(
        billing_webhooks.tap_gateway,
        "verify_webhook",
        lambda **kwargs: None,
    )
    client = TestClient(app)
    payload = {
        "id": "chg_paid_123",
        "status": "CAPTURED",
        "amount": 19.9,
        "currency": "USD",
        "metadata": {
            "xvond_company_id": "1",
            "xvond_service_subscription_id": str(subscription_id),
            "xvond_plan_id": "2",
            "xvond_service_code": "ai_agents",
        },
        "reference": {
            "gateway": "gw_paid",
            "payment": "pay_paid",
        },
        "transaction": {
            "created": "1760000000000",
            "url": "https://tap.example/pay/chg_paid_123",
        },
    }

    first = client.post(
        "/webhooks/billing/tap",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "hashstring": "test",
        },
    )
    second = client.post(
        "/webhooks/billing/tap",
        content=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "hashstring": "test",
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "processed"
    assert first.json()["result"]["charge_status"] == "captured"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    with factory() as db:
        subscription = db.get(ServiceSubscription, subscription_id)
        checkout = (
            db.query(ServiceCheckout)
            .filter(ServiceCheckout.provider == "tap")
            .one()
        )
        event = (
            db.query(ServicePaymentEvent)
            .filter(ServicePaymentEvent.provider == "tap")
            .one()
        )
        assert subscription.status == "active"
        assert checkout.status == "completed"
        assert event.company_id == 1
        assert event.service_checkout_id == checkout.id
        assert event.event_type == "charge.captured"
