from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_employee_builder as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent import self_service_policy
from backend.app.modules.channels.models import AgentChannel


USER = SimpleNamespace(company_id=1)
SPEC = {
    "scope": "business",
    "requirements": [
        {
            "key": "whatsapp",
            "kind": "channel",
            "purpose": "Talk with customers on WhatsApp",
            "status": "connection_required",
            "delivery_mode": "connect",
        }
    ],
    "delivery": {"provisioning_version": 1},
}


@pytest.fixture
def launch_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    monkeypatch.setattr(api.limits_service, "check_agent_limit", lambda *args: None)
    monkeypatch.setattr(api.limits_service, "check_channel_limit", lambda *args: None)
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

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service Owner",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            )
        )
        db.flush()
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Customer employee",
                description="Reply to customers on WhatsApp.",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add(
            CompanyModule(
                company_id=1,
                module_name="channels",
                enabled=True,
            )
        )
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={
                    "employee_builder": {
                        "onboarding_source": "self_service",
                        "delivery_mode": "self_service",
                        "source_description": "Reply to customers on WhatsApp.",
                        "requested_channels": ["whatsapp"],
                        "compiled_spec": SPEC,
                    }
                },
                capabilities={"customer_support": True},
                customer_controls={},
            )
        )
        db.add(
            AgentChannel(
                company_id=1,
                agent_id=1,
                channel_type="whatsapp",
                config={
                    "phone_number_id": "phone-1",
                    "access_token": "test-access",
                    "verify_token": "test-verify",
                    "app_secret": "test-secret",
                    "graph_api_version": "v26.0",
                },
                enabled=False,
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_draft_configured_channel_is_ready_for_atomic_launch(launch_database, monkeypatch):
    factory = launch_database
    monkeypatch.setattr(
        self_service_policy,
        "whatsapp_connection_state",
        lambda config, verify_remote=False: {
            "connected": True,
            "connection_issue": None,
        },
    )

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
        assert state["prepared_channels"] == ["whatsapp"]
        assert state["active_channels"] == []
        assert state["lifecycle"] == "draft"

    result = api.launch_self_service_employee(1, USER)
    assert result["status"] == "live"
    assert result["ready"] is True
    assert result["active_channels"] == ["whatsapp"]

    with factory() as db:
        company = db.get(Company, 1)
        agent = db.get(AIAgent, 1)
        channel = db.query(AgentChannel).filter_by(
            company_id=1,
            agent_id=1,
            channel_type="whatsapp",
        ).one()
        assert company.active is True
        assert company.lifecycle_status == "live"
        assert agent.enabled is True
        assert channel.enabled is True


def test_channel_activation_failure_rolls_back_entire_self_service_launch(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        self_service_policy,
        "whatsapp_connection_state",
        lambda config, verify_remote=False: {
            "connected": False,
            "connection_issue": "WhatsApp remote connection is not ready",
        },
    )

    with pytest.raises(HTTPException) as exc:
        api.launch_self_service_employee(1, USER)

    assert exc.value.status_code == 409
    assert exc.value.detail["message"] == "whatsapp is not ready for launch"
    assert exc.value.detail["blockers"] == [
        "WhatsApp remote connection is not ready"
    ]

    with factory() as db:
        company = db.get(Company, 1)
        agent = db.get(AIAgent, 1)
        channel = db.query(AgentChannel).filter_by(
            company_id=1,
            agent_id=1,
            channel_type="whatsapp",
        ).one()
        assert company.active is False
        assert company.lifecycle_status == "onboarding"
        assert agent.enabled is False
        assert channel.enabled is False


def test_future_catalog_channel_does_not_look_launch_ready_before_runtime_support(
    launch_database,
):
    factory = launch_database
    voice_spec = {
        "scope": "business",
        "requirements": [
            {
                "key": "voice",
                "kind": "channel",
                "status": "connection_required",
            }
        ],
        "delivery": {"provisioning_version": 1},
    }
    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = dict(config.settings)
        builder = dict(settings["employee_builder"])
        builder["requested_channels"] = ["voice"]
        builder["compiled_spec"] = voice_spec
        settings["employee_builder"] = builder
        config.settings = settings
        db.add(
            AgentChannel(
                company_id=1,
                agent_id=1,
                channel_type="voice",
                config={
                    "provider": "vapi",
                    "phone_number": "+10000000000",
                },
                enabled=False,
            )
        )
        db.commit()

    with factory() as db:
        state = self_service_policy.self_service_readiness(
            db,
            company=db.get(Company, 1),
            agent=db.get(AIAgent, 1),
            config=db.query(AgentConfig).filter_by(agent_id=1).one(),
        )

    assert state["ready"] is False
    assert state["prepared_channels"] == []
    assert state["missing_channels"] == ["voice"]


def test_self_service_deactivate_stops_channels_and_can_relaunch(
    launch_database,
    monkeypatch,
):
    factory = launch_database
    monkeypatch.setattr(
        self_service_policy,
        "whatsapp_connection_state",
        lambda config, verify_remote=False: {
            "connected": True,
            "connection_issue": None,
        },
    )

    api.launch_self_service_employee(1, USER)
    stopped = api.deactivate_self_service_employee(1, USER)

    assert stopped["status"] == "draft"
    assert stopped["company_lifecycle"] == "paused"
    assert stopped["deactivated_channels"] == ["whatsapp"]

    with factory() as db:
        company = db.get(Company, 1)
        agent = db.get(AIAgent, 1)
        channel = db.query(AgentChannel).filter_by(
            company_id=1,
            agent_id=1,
            channel_type="whatsapp",
        ).one()
        config = db.query(AgentConfig).filter_by(agent_id=1).one()

        assert company.active is False
        assert company.lifecycle_status == "paused"
        assert agent.enabled is False
        assert channel.enabled is False
        assert self_service_policy.self_service_readiness(
            db,
            company=company,
            agent=agent,
            config=config,
        )["ready"] is True

    restarted = api.launch_self_service_employee(1, USER)
    assert restarted["status"] == "live"
    assert restarted["active_channels"] == ["whatsapp"]
