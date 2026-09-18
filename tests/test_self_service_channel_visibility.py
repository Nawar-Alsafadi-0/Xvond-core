from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.api import ai_agents as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.self_service_policy import self_service_channel_slots


def test_self_service_channel_slots_follow_current_job_contract():
    builder = {
        "requested_channels": ["website", "website"],
        "compiled_spec": {
            "requirements": [
                {
                    "key": "whatsapp",
                    "kind": "channel",
                    "status": "connection_required",
                },
                {
                    "key": "knowledge",
                    "kind": "knowledge",
                    "status": "customer_input_required",
                },
            ]
        },
    }

    assert self_service_channel_slots(builder) == ["website", "whatsapp"]
    assert self_service_channel_slots({}) == []


@pytest.fixture
def agent_list_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        db.add_all(
            [
                Company(
                    id=1,
                    name="Self Service",
                    onboarding_source="self_service",
                    active=False,
                    lifecycle_status="onboarding",
                ),
                Company(
                    id=2,
                    name="Managed",
                    onboarding_source="managed",
                    active=False,
                    lifecycle_status="onboarding",
                ),
            ]
        )
        db.add_all(
            [
                AIAgent(
                    id=1,
                    company_id=1,
                    name="Website employee",
                    system_prompt="test",
                    provider="mock",
                    model="mock",
                    enabled=False,
                ),
                AIAgent(
                    id=2,
                    company_id=1,
                    name="Background employee",
                    system_prompt="test",
                    provider="mock",
                    model="mock",
                    enabled=False,
                ),
                AIAgent(
                    id=3,
                    company_id=2,
                    name="Managed employee",
                    system_prompt="test",
                    provider="mock",
                    model="mock",
                    enabled=False,
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                AgentConfig(
                    agent_id=1,
                    agent_type="employee",
                    settings={
                        "employee_builder": {
                            "requested_channels": ["website"],
                            "compiled_spec": {
                                "requirements": [
                                    {
                                        "key": "whatsapp",
                                        "kind": "channel",
                                        "status": "connection_required",
                                    }
                                ]
                            },
                        }
                    },
                    capabilities={},
                    customer_controls={},
                ),
                AgentConfig(
                    agent_id=2,
                    agent_type="employee",
                    settings={
                        "employee_builder": {
                            "requested_channels": [],
                            "compiled_spec": {
                                "scope": "personal",
                                "requirements": [],
                            },
                        }
                    },
                    capabilities={},
                    customer_controls={},
                ),
                AgentConfig(
                    agent_id=3,
                    agent_type="employee",
                    settings={
                        "employee_builder": {
                            "requested_channels": ["whatsapp"],
                        }
                    },
                    capabilities={},
                    customer_controls={},
                ),
            ]
        )
        db.commit()

    yield factory
    engine.dispose()


def test_agent_list_exposes_only_self_service_channel_slots(agent_list_database):
    self_service = api.list_agents(
        SimpleNamespace(company_id=1, role="owner")
    )
    assert self_service["agents"][0]["self_service_channel_slots"] == [
        "website",
        "whatsapp",
    ]
    assert self_service["agents"][1]["self_service_channel_slots"] == []

    managed = api.list_agents(
        SimpleNamespace(company_id=2, role="owner")
    )
    assert managed["agents"][0]["self_service_channel_slots"] is None


def test_customer_channel_ui_filters_self_service_cards_by_job_brief():
    website = Path("frontend/customer/website-channel.js").read_text(
        encoding="utf-8"
    )
    whatsapp = Path("frontend/customer/meta-whatsapp.js").read_text(
        encoding="utf-8"
    )

    assert 'slots.includes("website")' in website
    assert 'self_service_channel_slots' in website
    assert 'selfService && !slots.includes("whatsapp")' in whatsapp
    assert 'self_service_channel_slots' in whatsapp
