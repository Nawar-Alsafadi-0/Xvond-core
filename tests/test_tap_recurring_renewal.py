from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.api import billing_webhooks
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing import renewal
from backend.app.modules.billing.payment_gateway import (
    PaymentGatewayOutcomeUnknown,
    TapGateway,
)
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentProfile,
    ServicePlan,
    ServiceRenewalAttempt,
    ServiceSubscription,
)


@pytest.fixture
def renewal_database(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(renewal, "SessionLocal", factory)

    now = datetime(2026, 9, 18, 12, 0, 0)
    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Recurring Company",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Employee",
                description="Recurring test",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        plan = ServicePlan(
            id=1,
            service_code="ai_agents",
            tier="business",
            name="Business",
            monthly_price=Decimal("19.900"),
            currency="OMR",
            limits={"agents": 1},
            enabled=True,
        )
        db.add(plan)
        db.flush()
        subscription = ServiceSubscription(
            id=1,
            company_id=1,
            service_code="ai_agents",
            plan_id=1,
            status="active",
            current_period_start=now - timedelta(days=30),
            current_period_end=now + timedelta(hours=1),
        )
        db.add(subscription)
        db.commit()

    monkeypatch.setattr(renewal.settings, "BILLING_PROVIDER", "tap")
    monkeypatch.setattr(renewal.settings, "TAP_RECURRING_ENABLED", True)
    monkeypatch.setattr(renewal.settings, "TAP_SAVE_CARD_FOR_RECURRING", True)
    monkeypatch.setattr(renewal.settings, "TAP_RENEWAL_LEAD_HOURS", 24)
    monkeypatch.setattr(renewal.tap_gateway, "configured", lambda: True)

    yield factory, now
    engine.dispose()


def _add_profile(factory):
    from backend.app.core.config_secrets import protect_config

    with factory() as db:
        db.add(
            ServicePaymentProfile(
                company_id=1,
                service_subscription_id=1,
                provider="tap",
                status="active",
                provider_config=protect_config(
                    {
                        "provider_customer_token": "cus_live_123",
                        "provider_card_token": "card_live_456",
                        "payment_agreement_token": "payagree_live_789",
                        "card_brand": "VISA",
                        "card_last4": "4242",
                    }
                ),
            )
        )
        db.commit()


def test_tap_payment_profile_provider_references_are_encrypted(renewal_database):
    factory, _now = renewal_database
    _add_profile(factory)

    with factory() as db:
        profile = db.query(ServicePaymentProfile).one()
        stored = str(profile.provider_config)
        assert "cus_live_123" not in stored
        assert "card_live_456" not in stored
        assert "payagree_live_789" not in stored

        revealed = reveal_config(profile.provider_config)
        assert revealed["provider_customer_token"] == "cus_live_123"
        assert revealed["provider_card_token"] == "card_live_456"
        assert revealed["payment_agreement_token"] == "payagree_live_789"
        assert revealed["card_last4"] == "4242"


def test_tap_captured_webhook_builds_recurring_profile_from_provider_ids(
    renewal_database,
    monkeypatch,
):
    factory, now = renewal_database
    monkeypatch.setattr(billing_webhooks, "_utcnow_naive", lambda: now)

    with factory() as db:
        checkout = ServiceCheckout(
            id=1,
            company_id=1,
            service_subscription_id=1,
            plan_id=1,
            provider="tap",
            provider_transaction_id="chg_profile_1",
            provider_subscription_id=None,
            status="pending",
            checkout_url="https://tap.example/pay",
            amount=Decimal("19.900"),
            currency="OMR",
        )
        db.add(checkout)
        db.commit()

        result = billing_webhooks._process_tap_charge(
            db,
            {
                "id": "chg_profile_1",
                "status": "CAPTURED",
                "metadata": {
                    "xvond_company_id": "1",
                    "xvond_service_subscription_id": "1",
                    "xvond_plan_id": "1",
                    "xvond_service_code": "ai_agents",
                },
                "customer": {"id": "cus_profile"},
                "card": {
                    "id": "card_profile",
                    "brand": "VISA",
                    "last_four": "1111",
                },
                "payment_agreement": {"id": "payagree_profile"},
            },
        )
        db.commit()

        assert result["payment_profile_id"] is not None
        profile = db.get(ServicePaymentProfile, result["payment_profile_id"])
        revealed = reveal_config(profile.provider_config)
        assert revealed["provider_customer_token"] == "cus_profile"
        assert revealed["provider_card_token"] == "card_profile"
        assert revealed["payment_agreement_token"] == "payagree_profile"


def test_tap_saved_card_token_and_recurring_charge_use_provider_ids_only(monkeypatch):
    gateway = TapGateway()
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.BILLING_PROVIDER",
        "tap",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_SECRET_KEY",
        "sk_test_recurring",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.TAP_MERCHANT_ID",
        "merchant_1",
    )
    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.settings.PUBLIC_BASE_URL",
        "https://api.xvond.example",
    )

    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, *, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json})
        if url.endswith("/tokens/"):
            return Response({"id": "tok_one_time"})
        return Response({"id": "chg_renew_1", "status": "INITIATED"})

    monkeypatch.setattr(
        "backend.app.modules.billing.payment_gateway.httpx.post",
        fake_post,
    )

    result = gateway.create_recurring_charge(
        company_id=1,
        service_subscription_id=7,
        plan_id=3,
        plan_tier="business",
        service_code="ai_agents",
        amount=Decimal("19.900"),
        currency="OMR",
        customer_id="cus_123",
        card_id="card_456",
        payment_agreement_id="payagree_789",
        idempotency_key="renew-7-period",
    )

    assert len(calls) == 2
    token_call, charge_call = calls
    assert token_call["json"] == {
        "saved_card": {
            "card_id": "card_456",
            "customer_id": "cus_123",
        },
        "client_ip": "127.0.0.1",
    }
    body = charge_call["json"]
    assert body["customer_initiated"] is False
    assert body["threeDSecure"] is False
    assert body["payment_agreement"]["id"] == "payagree_789"
    assert body["source"]["id"] == "tok_one_time"
    assert body["customer"]["id"] == "cus_123"
    assert body["reference"]["idempotent"] == "renew-7-period"
    assert body["metadata"]["xvond_renewal"] == "true"
    assert "card" not in body
    assert result["transaction_id"] == "chg_renew_1"


def test_renewal_attempt_is_persisted_before_external_charge(
    renewal_database,
    monkeypatch,
):
    factory, now = renewal_database
    _add_profile(factory)
    observed = {}

    def create_charge(**kwargs):
        with factory() as db:
            attempt = db.query(ServiceRenewalAttempt).one()
            observed["status"] = attempt.status
            observed["attempts"] = attempt.attempts
            observed["key"] = attempt.idempotency_key
        return {
            "provider": "tap",
            "transaction_id": "chg_renew_submit",
            "status": "initiated",
        }

    monkeypatch.setattr(renewal.tap_gateway, "create_recurring_charge", create_charge)

    summary = renewal.run_due_service_renewals_once(now=now)

    assert summary["submitted"] == 1
    assert observed["status"] == "sending"
    assert observed["attempts"] == 1
    with factory() as db:
        attempt = db.query(ServiceRenewalAttempt).one()
        checkout = db.query(ServiceCheckout).one()
        subscription = db.get(ServiceSubscription, 1)
        assert attempt.status == "submitted"
        assert attempt.provider_transaction_id == "chg_renew_submit"
        assert checkout.provider_transaction_id == "chg_renew_submit"
        assert subscription.current_period_end == now + timedelta(hours=1)


def test_ambiguous_recurring_charge_is_not_automatically_retried(
    renewal_database,
    monkeypatch,
):
    factory, now = renewal_database
    _add_profile(factory)
    calls = {"count": 0}

    def ambiguous(**kwargs):
        calls["count"] += 1
        raise PaymentGatewayOutcomeUnknown("timeout")

    monkeypatch.setattr(renewal.tap_gateway, "create_recurring_charge", ambiguous)

    first = renewal.run_due_service_renewals_once(now=now)
    second = renewal.run_due_service_renewals_once(now=now)

    assert first["unknown"] == 1
    assert second["unknown"] == 1
    assert calls["count"] == 1
    with factory() as db:
        attempt = db.query(ServiceRenewalAttempt).one()
        assert attempt.status == "unknown"
        assert attempt.attempts == 1


def test_due_renewal_without_active_payment_profile_is_blocked(
    renewal_database,
    monkeypatch,
):
    _factory, now = renewal_database
    monkeypatch.setattr(
        renewal.tap_gateway,
        "create_recurring_charge",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("provider must not be called")
        ),
    )

    summary = renewal.run_due_service_renewals_once(now=now)

    assert summary["checked"] == 1
    assert summary["blocked"] == 1
    assert summary["submitted"] == 0


def test_recurring_scheduler_is_disabled_until_both_tap_flags_are_enabled(
    renewal_database,
    monkeypatch,
):
    _factory, now = renewal_database
    monkeypatch.setattr(renewal.settings, "TAP_RECURRING_ENABLED", False)

    summary = renewal.run_due_service_renewals_once(now=now)

    assert summary["enabled"] is False
    assert summary["checked"] == 0


def test_sync_captured_response_stays_submitted_until_verified_webhook(
    renewal_database,
    monkeypatch,
):
    factory, now = renewal_database
    _add_profile(factory)
    original_end = now + timedelta(hours=1)

    monkeypatch.setattr(
        renewal.tap_gateway,
        "create_recurring_charge",
        lambda **kwargs: {
            "provider": "tap",
            "transaction_id": "chg_sync_captured",
            "status": "captured",
        },
    )

    summary = renewal.run_due_service_renewals_once(now=now)

    assert summary["submitted"] == 1
    assert summary["captured"] == 0
    with factory() as db:
        attempt = db.query(ServiceRenewalAttempt).one()
        subscription = db.get(ServiceSubscription, 1)
        checkout = db.query(ServiceCheckout).one()
        assert attempt.status == "submitted"
        assert checkout.status == "pending"
        assert subscription.current_period_end == original_end


def test_verified_recurring_webhook_advances_period_and_captures_attempt(
    renewal_database,
    monkeypatch,
):
    factory, now = renewal_database
    _add_profile(factory)
    monkeypatch.setattr(
        renewal.tap_gateway,
        "create_recurring_charge",
        lambda **kwargs: {
            "provider": "tap",
            "transaction_id": "chg_webhook_renewal",
            "status": "initiated",
        },
    )

    renewal.run_due_service_renewals_once(now=now)

    with factory() as db:
        subscription = db.get(ServiceSubscription, 1)
        old_end = subscription.current_period_end
        result = billing_webhooks._process_tap_charge(
            db,
            {
                "id": "chg_webhook_renewal",
                "status": "CAPTURED",
                "metadata": {
                    "xvond_company_id": "1",
                    "xvond_service_subscription_id": "1",
                    "xvond_plan_id": "1",
                    "xvond_service_code": "ai_agents",
                    "xvond_renewal": "true",
                },
                "customer": {"id": "cus_live_123"},
                "card": {"id": "card_live_456"},
                "payment_agreement": {"id": "payagree_live_789"},
            },
        )
        db.commit()

        attempt = db.query(ServiceRenewalAttempt).one()
        subscription = db.get(ServiceSubscription, 1)
        assert result["renewal_attempt_id"] == attempt.id
        assert attempt.status == "captured"
        assert subscription.current_period_start == old_end
        assert subscription.current_period_end > old_end
