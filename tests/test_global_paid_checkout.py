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
from backend.app.modules.billing.payment_gateway import PaddleGateway
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentEvent,
    ServicePlan,
    ServiceSubscription,
)


OWNER = SimpleNamespace(company_id=1, role="owner", id=10)


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
        assert db.query(ServicePaymentEvent).count() == 1
        entitlement, plan = service_limits.entitlement(db, 1, "ai_agents")
        assert entitlement.id == subscription_id
        assert plan.id == 2
