from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app
from backend.app.api import website_widget as api
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent import self_service_policy
from backend.app.modules.channels.models import AgentChannel


USER = SimpleNamespace(company_id=1, role="owner")


@pytest.fixture
def website_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

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
                name="Other",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            ),
            Company(
                id=3,
                name="Managed",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="managed",
            ),
        ])
        db.flush()
        db.add_all([
            AIAgent(
                id=1,
                company_id=1,
                name="Web employee",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=False,
            ),
            AIAgent(
                id=2,
                company_id=2,
                name="Other employee",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=False,
            ),
            AIAgent(
                id=3,
                company_id=3,
                name="Managed employee",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=False,
            ),
        ])
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={
                    "employee_builder": {
                        "onboarding_source": "self_service",
                        "delivery_mode": "self_service",
                        "requested_channels": ["website"],
                        "compiled_spec": None,
                    }
                },
                capabilities={},
                customer_controls={},
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_customer_can_prepare_website_channel_without_exposing_widget_key(website_database):
    factory = website_database

    result = api.customer_configure_website(
        1,
        api.WebsiteSetup(
            allowed_domain="https://Example.com/path",
            widget_name="Website Assistant",
        ),
        USER,
    )

    assert result["status"] == "configured"
    assert result["prepared"] is True
    assert result["enabled"] is False
    assert result["config"]["allowed_domain"] == "example.com"
    assert result["config"]["widget_name"] == "Website Assistant"
    assert "widget_key" not in result["config"]
    assert "<script" in result["embed_code"]

    with factory() as db:
        channel = db.query(AgentChannel).filter_by(
            company_id=1,
            agent_id=1,
            channel_type="website",
        ).one()
        stored = reveal_config(channel.config)
        assert stored["widget_key"]
        assert stored["allowed_domain"] == "example.com"
        assert channel.enabled is False
        module = db.query(CompanyModule).filter_by(
            company_id=1,
            module_name="channels",
        ).one()
        assert module.enabled is True

    status = api.customer_get_website_config(1, USER)
    assert status["configured"] is True
    assert status["prepared"] is True
    assert status["can_edit"] is True
    assert "widget_key" not in status["config"]


def test_customer_website_update_preserves_widget_identity(website_database):
    factory = website_database
    first = api.customer_configure_website(
        1,
        api.WebsiteSetup(allowed_domain="example.com"),
        USER,
    )
    with factory() as db:
        original_key = reveal_config(
            db.query(AgentChannel).filter_by(id=first["channel_id"]).one().config
        )["widget_key"]

    api.customer_configure_website(
        1,
        api.WebsiteSetup(
            allowed_domain="shop.example.com",
            widget_name="Store Assistant",
        ),
        USER,
    )

    with factory() as db:
        channel = db.query(AgentChannel).filter_by(id=first["channel_id"]).one()
        stored = reveal_config(channel.config)
        assert stored["widget_key"] == original_key
        assert stored["allowed_domain"] == "shop.example.com"
        assert stored["widget_name"] == "Store Assistant"


def test_live_employee_must_be_deactivated_before_customer_website_edit(website_database):
    factory = website_database
    api.customer_configure_website(
        1,
        api.WebsiteSetup(allowed_domain="example.com"),
        USER,
    )
    with factory() as db:
        db.get(AIAgent, 1).enabled = True
        db.commit()

    with pytest.raises(HTTPException) as exc:
        api.customer_configure_website(
            1,
            api.WebsiteSetup(allowed_domain="changed.example.com"),
            USER,
        )
    assert exc.value.status_code == 409
    assert "Deactivate" in str(exc.value.detail)

    with factory() as db:
        channel = db.query(AgentChannel).filter_by(
            company_id=1,
            agent_id=1,
            channel_type="website",
        ).one()
        assert reveal_config(channel.config)["allowed_domain"] == "example.com"


def test_customer_website_setup_rejects_unselected_self_service_channel(
    website_database,
):
    user = SimpleNamespace(company_id=2, role="owner")
    with pytest.raises(HTTPException) as exc_info:
        api.customer_get_website_config(2, user)
    assert exc_info.value.status_code == 409
    assert "current Job Brief" in str(exc_info.value.detail)


def test_customer_website_setup_is_tenant_scoped_and_self_service_only(website_database):
    with pytest.raises(HTTPException) as other:
        api.customer_get_website_config(2, USER)
    assert other.value.status_code == 404

    managed_user = SimpleNamespace(company_id=3, role="owner")
    with pytest.raises(HTTPException) as managed:
        api.customer_get_website_config(3, managed_user)
    assert managed.value.status_code == 409


def test_customer_website_setup_flows_into_atomic_self_service_launch(
    website_database,
    monkeypatch,
):
    factory = website_database
    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        config.settings = {
            "employee_builder": {
                "onboarding_source": "self_service",
                "delivery_mode": "self_service",
                "source_description": "Reply to website visitors.",
                "requested_channels": ["website"],
                "compiled_spec": {
                    "scope": "business",
                    "requirements": [
                        {
                            "key": "website",
                            "kind": "channel",
                            "status": "connection_required",
                        }
                    ],
                    "delivery": {"provisioning_version": 1},
                },
            }
        }
        config.capabilities = {"customer_support": True}
        db.commit()

    monkeypatch.setattr(
        self_service_policy,
        "subscription_snapshot",
        lambda *args, **kwargs: {
            "active": True,
            "subscription": SimpleNamespace(id=1),
            "plan": SimpleNamespace(id=1),
            "plan_name": "Self Service",
            "plan_tier": "starter",
            "channel_limit": 1,
        },
    )
    monkeypatch.setattr(api.limits_service, "check_agent_limit", lambda *args: None)
    monkeypatch.setattr(api.limits_service, "check_channel_limit", lambda *args: None)

    prepared = api.customer_configure_website(
        1,
        api.WebsiteSetup(allowed_domain="example.com"),
        USER,
    )
    assert prepared["prepared"] is True
    assert prepared["enabled"] is False

    with factory() as db:
        company = db.get(Company, 1)
        agent = db.get(AIAgent, 1)
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        state = self_service_policy.self_service_readiness(
            db,
            company=company,
            agent=agent,
            config=config,
        )
        assert state["ready"] is True
        assert state["prepared_channels"] == ["website"]
        assert state["active_channels"] == []

    # Customer Website setup and Employee Builder launch share the same DB in
    # production; point the builder module at this isolated test factory too.
    monkeypatch.setattr(
        "backend.app.api.customer_employee_builder.SessionLocal",
        factory,
    )
    launched = __import__(
        "backend.app.api.customer_employee_builder",
        fromlist=["launch_self_service_employee"],
    ).launch_self_service_employee(1, USER)

    assert launched["status"] == "live"
    assert launched["active_channels"] == ["website"]
    with factory() as db:
        channel = db.query(AgentChannel).filter_by(
            company_id=1,
            agent_id=1,
            channel_type="website",
        ).one()
        assert channel.enabled is True
        assert db.get(AIAgent, 1).enabled is True
        assert db.get(Company, 1).active is True


def test_customer_website_routes_require_authentication():
    client = TestClient(app)
    response = client.get("/customer/website-channel/agents/1")
    assert response.status_code == 401


def test_customer_portal_loads_website_setup_ui():
    from pathlib import Path

    index = Path("frontend/customer/index.html").read_text(encoding="utf-8")
    js = Path("frontend/customer/website-channel.js").read_text(encoding="utf-8")

    assert "/static/customer/website-channel.js" in index
    assert "/customer/website-channel/agents/" in js
    assert "Website Chat prepared" in js
    assert "Save Website setup" in js
    assert "widget_key" not in js
