from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.app.api import admin_service_billing
from backend.app.api import customer_subscription as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.audit.models import AuditLog
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription


OWNER = SimpleNamespace(company_id=1, role="owner", id=10)
ADMIN = SimpleNamespace(company_id=None, role="xvond_admin", id=99)


@pytest.fixture
def subscription_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    monkeypatch.setattr(admin_service_billing, "SessionLocal", factory)

    with factory() as db:
        db.add_all([
            Company(
                id=1,
                name="Self Service",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            ),
            Company(
                id=2,
                name="Managed",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="managed",
            ),
        ])
        db.flush()
        db.add_all([
            ServicePlan(
                id=1,
                service_code="ai_agents",
                tier="starter",
                name="Starter Free",
                monthly_price=Decimal("0"),
                currency="OMR",
                limits={"agents": 1, "channels": 1},
                enabled=True,
            ),
            ServicePlan(
                id=2,
                service_code="ai_agents",
                tier="business",
                name="Business",
                monthly_price=Decimal("19.900"),
                currency="OMR",
                limits={"agents": 1, "channels": 2},
                enabled=True,
            ),
            ServicePlan(
                id=3,
                service_code="ai_agents",
                tier="enterprise",
                name="Disabled",
                monthly_price=Decimal("50"),
                currency="OMR",
                limits={},
                enabled=False,
            ),
        ])
        db.commit()

    yield factory
    engine.dispose()


def test_paid_plan_request_stays_pending_without_entitlement(subscription_database):
    factory = subscription_database

    result = api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )

    assert result["status"] == "pending_payment"
    assert result["requires_payment"] is True
    assert result["subscription"]["plan"]["id"] == 2

    with factory() as db:
        item = db.query(ServiceSubscription).filter_by(
            company_id=1,
            service_code="ai_agents",
        ).one()
        assert item.status == "pending_payment"
        with pytest.raises(HTTPException) as exc:
            service_limits.entitlement(db, 1, "ai_agents")
        assert exc.value.status_code == 403
        audit = db.query(AuditLog).filter_by(
            action="service_subscription.customer_requested",
        ).one()
        assert audit.company_id == 1
        assert audit.details["plan_id"] == 2


def test_free_plan_activates_immediately_and_grants_entitlement(subscription_database):
    factory = subscription_database

    result = api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=1),
        OWNER,
    )

    assert result["status"] == "active"
    assert result["requires_payment"] is False

    with factory() as db:
        subscription, plan = service_limits.entitlement(db, 1, "ai_agents")
        assert subscription.status == "active"
        assert plan.id == 1
        assert db.query(AuditLog).filter_by(
            action="service_subscription.customer_activated_free",
        ).count() == 1


def test_pending_payment_is_exposed_in_customer_plan_status(subscription_database):
    api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )

    result = api.self_service_ai_agent_plans(OWNER)

    assert [item["id"] for item in result["plans"]] == [1, 2]
    assert result["online_payments_enabled"] is False
    assert result["subscription"]["status"] == "pending_payment"
    assert result["subscription"]["plan"]["id"] == 2


def test_repeated_paid_plan_request_is_idempotent(subscription_database):
    factory = subscription_database

    first = api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )
    second = api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )

    assert first["status"] == "pending_payment"
    assert second["status"] == "pending_payment"
    assert second["subscription"]["id"] == first["subscription"]["id"]

    with factory() as db:
        assert db.query(ServiceSubscription).filter_by(
            company_id=1,
            service_code="ai_agents",
        ).count() == 1
        assert db.query(AuditLog).filter_by(
            action="service_subscription.customer_requested",
        ).count() == 1


def test_active_subscription_cannot_be_silently_replaced(subscription_database):
    api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=1),
        OWNER,
    )

    with pytest.raises(HTTPException) as exc:
        api.request_self_service_ai_agent_subscription(
            api.SelfServiceSubscriptionRequest(plan_id=2),
            OWNER,
        )

    assert exc.value.status_code == 409
    assert "active" in str(exc.value.detail).lower()


def test_disabled_plan_and_managed_workspace_are_rejected(subscription_database):
    with pytest.raises(HTTPException) as disabled:
        api.request_self_service_ai_agent_subscription(
            api.SelfServiceSubscriptionRequest(plan_id=3),
            OWNER,
        )
    assert disabled.value.status_code == 404

    managed = SimpleNamespace(company_id=2, role="owner", id=11)
    with pytest.raises(HTTPException) as managed_exc:
        api.self_service_ai_agent_plans(managed)
    assert managed_exc.value.status_code == 409


def test_admin_activation_of_pending_payment_starts_fresh_billing_period(
    subscription_database,
    monkeypatch,
):
    factory = subscription_database
    api.request_self_service_ai_agent_subscription(
        api.SelfServiceSubscriptionRequest(plan_id=2),
        OWNER,
    )

    activation_time = datetime(2026, 10, 1, 12, 0, 0)
    monkeypatch.setattr(
        admin_service_billing,
        "_utcnow_naive",
        lambda: activation_time,
    )

    result = admin_service_billing.set_service_status(
        1,
        "ai_agents",
        admin_service_billing.ServiceStatusInput(status="active"),
        ADMIN,
    )

    assert result["status"] == "active"
    with factory() as db:
        item = db.query(ServiceSubscription).filter_by(
            company_id=1,
            service_code="ai_agents",
        ).one()
        assert item.current_period_start == activation_time
        assert item.current_period_end > activation_time


def test_customer_subscription_routes_require_authentication():
    client = TestClient(app)
    assert client.get("/customer/subscription/ai-agents/plans").status_code == 401
    assert client.post(
        "/customer/subscription/ai-agents/request",
        json={"plan_id": 1},
    ).status_code == 401


def test_employee_builder_exposes_plan_selection_and_pending_payment_state():
    from pathlib import Path

    js = Path("frontend/customer/employee-builder.js").read_text(encoding="utf-8")
    builder_api = Path("backend/app/api/customer_employee_builder.py").read_text(encoding="utf-8")
    assert "/customer/subscription/ai-agents/plans" in js
    assert "/customer/subscription/ai-agents/request" in js
    assert "Payment pending" in builder_api
    assert "Activate free plan" in js
    assert "Online payment is not enabled yet" in js
    assert "A company Owner or Admin must choose the subscription plan." in js
    assert '["owner", "admin"]' in js
