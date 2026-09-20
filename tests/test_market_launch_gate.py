from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentEvent,
    ServicePaymentProfile,
    ServicePlan,
    ServiceRenewalAttempt,
    ServiceSubscription,
)
from backend.app.modules.channels.models import AgentChannel
from scripts import market_launch_gate as gate


@pytest.fixture
def launch_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(gate, "SessionLocal", factory)

    now = datetime.utcnow()
    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Launch Company",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Launch Employee",
                description="Handle customers",
                system_prompt="Help customers",
                provider="openai",
                model="gpt-test",
                enabled=True,
            )
        )
        plan = ServicePlan(
            id=1,
            service_code="ai_agents",
            tier="business",
            name="Business",
            monthly_price=Decimal("19.900"),
            currency="USD",
            limits={"agents": 1, "channels": 5},
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
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=30),
        )
        db.add(subscription)
        db.add(
            AgentChannel(
                id=10,
                company_id=1,
                agent_id=1,
                channel_type="telegram",
                enabled=True,
                config={
                    "connection_key": "telegram-main",
                    "provisioning_state": "connected",
                },
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_market_gate_requires_real_roundtrip_for_required_packaged_channel(
    launch_database,
    monkeypatch,
):
    monkeypatch.setattr(
        gate,
        "check_release",
        lambda **kwargs: {"overall_ok": True},
    )
    monkeypatch.setattr(
        gate,
        "_billing_gate",
        lambda *args, **kwargs: {"ok": True},
    )

    report = gate.market_launch_gate(
        company_id=1,
        agent_id=1,
        launch_mode="self_service",
        required_channels=["telegram"],
        require_online_billing=False,
        require_payment_evidence=False,
    )

    assert report["launchable"] is False
    assert report["channels"]["channels"]["telegram"]["reason"] == "real_customer_roundtrip_missing"

    with launch_database() as db:
        channel = db.get(AgentChannel, 10)
        channel.customer_roundtrip_verified_at = datetime.utcnow()
        channel.customer_roundtrip_source = "telegram_provider_confirmed"
        db.commit()

    report = gate.market_launch_gate(
        company_id=1,
        agent_id=1,
        launch_mode="self_service",
        required_channels=["telegram"],
        require_online_billing=False,
        require_payment_evidence=False,
    )

    assert report["launchable"] is True
    assert report["channels"]["channels"]["telegram"]["roundtrip_verified"] is True


def test_market_gate_rejects_non_packaged_channel_as_launch_requirement(
    launch_database,
    monkeypatch,
):
    monkeypatch.setattr(
        gate,
        "check_release",
        lambda **kwargs: {"overall_ok": True},
    )
    monkeypatch.setattr(
        gate,
        "_billing_gate",
        lambda *args, **kwargs: {"ok": True},
    )
    with launch_database() as db:
        db.add(
            AgentChannel(
                company_id=1,
                agent_id=1,
                channel_type="teams",
                enabled=True,
                config={
                    "connection_key": "teams-custom",
                    "provisioning_state": "connected",
                },
            )
        )
        db.commit()

    report = gate.market_launch_gate(
        company_id=1,
        agent_id=1,
        launch_mode="self_service",
        required_channels=["teams"],
        require_online_billing=False,
        require_payment_evidence=False,
    )

    item = report["channels"]["channels"]["teams"]
    assert report["launchable"] is False
    assert item["packaged_provider"] is False
    assert item["reason"] == "provider_not_packaged"


def test_payment_evidence_is_scoped_to_same_company_and_checkout(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        gate,
        "payment_gateway",
        lambda: SimpleNamespace(provider="paddle", configured=lambda: True),
    )
    monkeypatch.setattr(gate.settings, "BILLING_PROVIDER", "paddle")
    monkeypatch.setattr(gate.settings, "PADDLE_ENVIRONMENT", "live")
    monkeypatch.setattr(gate.settings, "PADDLE_CHECKOUT_URL", "https://xvond.example/checkout")

    with factory() as db:
        checkout = ServiceCheckout(
            id=1,
            company_id=1,
            service_subscription_id=1,
            plan_id=1,
            provider="paddle",
            provider_transaction_id="txn-company-1",
            provider_subscription_id="sub-company-1",
            status="completed",
            checkout_url="https://checkout.example",
            amount=Decimal("19.900"),
            currency="USD",
        )
        db.add(checkout)
        db.flush()
        db.add(
            ServicePaymentEvent(
                company_id=999,
                service_checkout_id=None,
                provider="paddle",
                provider_event_id="evt-other-company",
                event_type="transaction.completed",
            )
        )
        db.commit()

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=True,
        )
    assert result["ok"] is False
    assert result["completed_checkout_evidence"] is True
    assert result["signed_payment_webhook_evidence"] is False

    with factory() as db:
        db.add(
            ServicePaymentEvent(
                company_id=1,
                service_checkout_id=1,
                provider="paddle",
                provider_event_id="evt-company-1",
                event_type="transaction.completed",
            )
        )
        db.commit()

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=True,
        )
    assert result["ok"] is True
    assert result["signed_payment_webhook_evidence"] is True


def test_market_gate_launch_mode_must_match_company_source(
    launch_database,
    monkeypatch,
):
    monkeypatch.setattr(gate, "check_release", lambda **kwargs: {"overall_ok": True})
    monkeypatch.setattr(gate, "_channel_gate", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(gate, "_billing_gate", lambda *args, **kwargs: {"ok": True})

    report = gate.market_launch_gate(
        company_id=1,
        agent_id=1,
        launch_mode="managed",
        required_channels=["telegram"],
        require_online_billing=False,
        require_payment_evidence=False,
    )

    assert report["launchable"] is False
    assert report["identity"]["actual_launch_mode"] == "self_service"


def test_tap_payment_evidence_can_satisfy_provider_neutral_launch_gate(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        gate,
        "payment_gateway",
        lambda: SimpleNamespace(provider="tap", configured=lambda: True),
    )
    monkeypatch.setattr(gate.settings, "BILLING_PROVIDER", "tap")
    monkeypatch.setattr(gate.settings, "TAP_SECRET_KEY", "sk_live_tap_test")
    monkeypatch.setattr(gate.settings, "TAP_MERCHANT_ID", "merchant_live")
    monkeypatch.setattr(gate.settings, "PUBLIC_BASE_URL", "https://api.xvond.example")
    monkeypatch.setattr(gate.settings, "TAP_REDIRECT_URL", "")

    with factory() as db:
        checkout = ServiceCheckout(
            id=2,
            company_id=1,
            service_subscription_id=1,
            plan_id=1,
            provider="tap",
            provider_transaction_id="chg-company-1",
            provider_subscription_id=None,
            status="completed",
            checkout_url="https://tap.example/pay/chg-company-1",
            amount=Decimal("19.900"),
            currency="USD",
        )
        db.add(checkout)
        db.flush()
        db.add(
            ServicePaymentEvent(
                company_id=1,
                service_checkout_id=checkout.id,
                provider="tap",
                provider_event_id="chg-company-1:CAPTURED:1760000000000",
                event_type="charge.captured",
            )
        )
        db.commit()

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=True,
        )

    assert result["ok"] is True
    assert result["payment_provider"] == "tap"
    assert result["completed_checkout_evidence"] is True
    assert result["signed_payment_webhook_evidence"] is True


def test_tap_launch_gate_rejects_test_secret_for_production_acceptance(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        gate,
        "payment_gateway",
        lambda: SimpleNamespace(provider="tap", configured=lambda: True),
    )
    monkeypatch.setattr(gate.settings, "BILLING_PROVIDER", "tap")
    monkeypatch.setattr(gate.settings, "TAP_SECRET_KEY", "sk_test_tap")
    monkeypatch.setattr(gate.settings, "TAP_MERCHANT_ID", "merchant_test")
    monkeypatch.setattr(gate.settings, "PUBLIC_BASE_URL", "https://api.xvond.example")
    monkeypatch.setattr(gate.settings, "TAP_REDIRECT_URL", "")

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=False,
        )

    assert result["ok"] is False
    assert "tap_secret_key_not_live" in result["blockers"]


def test_tap_recurring_launch_gate_requires_payment_profile_when_enabled(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        gate,
        "payment_gateway",
        lambda: SimpleNamespace(provider="tap", configured=lambda: True),
    )
    monkeypatch.setattr(gate.settings, "BILLING_PROVIDER", "tap")
    monkeypatch.setattr(gate.settings, "TAP_SECRET_KEY", "sk_live_tap_test")
    monkeypatch.setattr(gate.settings, "TAP_MERCHANT_ID", "merchant_live")
    monkeypatch.setattr(gate.settings, "PUBLIC_BASE_URL", "https://api.xvond.example")
    monkeypatch.setattr(gate.settings, "TAP_REDIRECT_URL", "")
    monkeypatch.setattr(gate.settings, "TAP_RECURRING_ENABLED", True)
    monkeypatch.setattr(gate.settings, "TAP_SAVE_CARD_FOR_RECURRING", True)

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=False,
        )
    assert result["ok"] is False
    assert "tap_recurring_payment_profile_missing" in result["blockers"]

    with factory() as db:
        db.add(
            ServicePaymentProfile(
                company_id=1,
                service_subscription_id=1,
                provider="tap",
                status="active",
                provider_config={"encrypted_test": "value"},
            )
        )
        db.commit()

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=False,
        )
    assert result["ok"] is True
    assert result["tap_recurring_profile_ready"] is True


def test_tap_recurring_launch_gate_blocks_unresolved_renewal_attempt(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        gate,
        "payment_gateway",
        lambda: SimpleNamespace(provider="tap", configured=lambda: True),
    )
    monkeypatch.setattr(gate.settings, "BILLING_PROVIDER", "tap")
    monkeypatch.setattr(gate.settings, "TAP_SECRET_KEY", "sk_live_tap_test")
    monkeypatch.setattr(gate.settings, "TAP_MERCHANT_ID", "merchant_live")
    monkeypatch.setattr(gate.settings, "PUBLIC_BASE_URL", "https://api.xvond.example")
    monkeypatch.setattr(gate.settings, "TAP_REDIRECT_URL", "")
    monkeypatch.setattr(gate.settings, "TAP_RECURRING_ENABLED", True)
    monkeypatch.setattr(gate.settings, "TAP_SAVE_CARD_FOR_RECURRING", True)

    with factory() as db:
        db.add(
            ServicePaymentProfile(
                company_id=1,
                service_subscription_id=1,
                provider="tap",
                status="active",
                provider_config={"encrypted_test": "value"},
            )
        )
        subscription = db.get(ServiceSubscription, 1)
        db.add(
            ServiceRenewalAttempt(
                company_id=1,
                service_subscription_id=1,
                provider="tap",
                idempotency_key="renewal-unknown",
                period_end=subscription.current_period_end,
                status="unknown",
                attempts=1,
            )
        )
        db.commit()

    with factory() as db:
        result = gate._billing_gate(
            db,
            company_id=1,
            require_online_billing=True,
            require_payment_evidence=False,
        )
    assert result["ok"] is False
    assert result["unresolved_renewal_attempts"] == 1
    assert "tap_unresolved_renewal_attempts" in result["blockers"]
