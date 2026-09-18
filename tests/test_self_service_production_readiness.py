from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.core import readiness
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent import self_service_policy
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.profile_models import AIAgentProfile
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument


def _compiled_spec(requirements=None):
    return {
        "scope": "personal",
        "requirements": list(requirements or []),
        "delivery": {"provisioning_version": 1},
    }


@pytest.fixture
def self_service_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)

    now = datetime.utcnow()
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
        db.add_all(
            [
                CompanyModule(company_id=1, module_name="ai_agent", enabled=True),
                CompanyModule(company_id=1, module_name="knowledge", enabled=True),
                CompanyModule(company_id=1, module_name="tools", enabled=True),
                ServicePlan(
                    id=1,
                    service_code="ai_agents",
                    tier="starter",
                    name="Starter",
                    monthly_price=Decimal("0"),
                    currency="OMR",
                    limits={"agents": 1, "channels": 1},
                    enabled=True,
                ),
            ]
        )
        db.flush()
        db.add(
            ServiceSubscription(
                company_id=1,
                service_code="ai_agents",
                plan_id=1,
                status="active",
                current_period_start=now - timedelta(days=1),
                current_period_end=now + timedelta(days=30),
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Background employee",
                description="Monitor my work in the background.",
                system_prompt="Do the requested background job.",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add(
            AIAgentProfile(
                company_id=1,
                agent_id=1,
                business_name="Self Service",
                business_type="personal",
                reply_language="auto",
                conversation_style="professional_friendly",
                instructions="Monitor my work in the background.",
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
                        "requested_channels": [],
                        "compiled_spec": _compiled_spec(),
                    }
                },
                capabilities={},
                customer_controls={},
            )
        )
        db.commit()

    monkeypatch.setattr(
        readiness,
        "_provider_runtime",
        lambda *args, **kwargs: (
            [SimpleNamespace(provider="openai", model="gpt-test", reason="test")],
            None,
        ),
    )

    yield factory
    engine.dispose()


def test_self_service_background_readiness_does_not_require_managed_profile_knowledge_or_channel(
    self_service_database,
):
    factory = self_service_database

    with factory() as db:
        result = readiness.company_readiness(db, 1)

    assert result["delivery_mode"] == "self_service"
    assert result["company_profile_ready"] is False
    assert result["setup_ready"] is True
    assert result["ready_for_customer"] is False
    employee = result["agents"][0]
    assert employee["delivery_mode"] == "self_service"
    assert employee["setup_ready"] is True
    assert employee["configured_channel_count"] == 0
    assert employee["knowledge_count"] == 0
    assert "No enabled knowledge connected" not in employee["issues"]
    assert "No channel configured" not in employee["issues"]


def test_live_self_service_background_employee_is_service_ready_without_customer_channel(
    self_service_database,
):
    factory = self_service_database
    with factory() as db:
        company = db.get(Company, 1)
        agent = db.get(AIAgent, 1)
        company.active = True
        company.lifecycle_status = "live"
        agent.enabled = True
        db.commit()

    with factory() as db:
        result = readiness.company_readiness(db, 1)

    assert result["setup_ready"] is True
    assert result["ready_for_customer"] is True
    employee = result["agents"][0]
    assert employee["ready_for_customer"] is True
    assert employee["self_service_readiness"]["channels_required"] is False
    assert "AI employee is live but no customer channel is active" not in employee["warnings"]


def test_self_service_knowledge_requirement_resolves_only_after_enabled_knowledge_is_attached(
    self_service_database,
):
    factory = self_service_database
    knowledge_spec = _compiled_spec(
        [
            {
                "key": "knowledge",
                "kind": "knowledge",
                "status": "customer_input_required",
                "purpose": "Use the owner's reference material",
            }
        ]
    )

    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        settings = dict(config.settings)
        builder = dict(settings["employee_builder"])
        builder["compiled_spec"] = knowledge_spec
        settings["employee_builder"] = builder
        config.settings = settings
        db.commit()

    with factory() as db:
        state = self_service_policy.self_service_readiness(
            db,
            company=db.get(Company, 1),
            agent=db.get(AIAgent, 1),
            config=db.query(AgentConfig).filter_by(agent_id=1).one(),
        )
    assert state["ready"] is False
    assert "knowledge: setup required" in state["blockers"]
    assert "knowledge" not in state["resolved_requirements"]

    with factory() as db:
        db.add(
            Company(
                id=9,
                name="Other tenant",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            )
        )
        db.flush()
        foreign_document = KnowledgeDocument(
            company_id=9,
            title="Wrong tenant knowledge",
            source_type="text",
            content="This must never satisfy another tenant.",
            enabled=True,
        )
        db.add(foreign_document)
        db.flush()
        db.add(
            AgentKnowledge(
                agent_id=1,
                document_id=foreign_document.id,
                enabled=True,
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
    assert "knowledge: setup required" in state["blockers"]
    assert "knowledge" not in state["resolved_requirements"]

    with factory() as db:
        document = KnowledgeDocument(
            company_id=1,
            title="Owner rules",
            source_type="text",
            content="Important operating rules.",
            enabled=True,
        )
        db.add(document)
        db.flush()
        db.add(AgentKnowledge(agent_id=1, document_id=document.id, enabled=True))
        db.commit()

    with factory() as db:
        state = self_service_policy.self_service_readiness(
            db,
            company=db.get(Company, 1),
            agent=db.get(AIAgent, 1),
            config=db.query(AgentConfig).filter_by(agent_id=1).one(),
        )
    assert state["ready"] is True
    assert "knowledge" in state["resolved_requirements"]
    assert "knowledge: setup required" not in state["blockers"]


def test_production_self_service_requires_real_provider_even_without_channel(monkeypatch):
    monkeypatch.setattr(self_service_policy.settings, "APP_ENV", "production")
    monkeypatch.setattr(
        self_service_policy,
        "runtime_selections",
        lambda *args, **kwargs: [
            SimpleNamespace(provider="mock", model="mock")
        ],
    )
    agent = SimpleNamespace(provider="mock", model="mock")

    assert self_service_policy._self_service_provider_ready(
        object(),
        company_id=1,
        agent=agent,
    ) is False

    monkeypatch.setattr(
        self_service_policy,
        "runtime_selections",
        lambda *args, **kwargs: [
            SimpleNamespace(provider="openai", model="gpt-test")
        ],
    )
    assert self_service_policy._self_service_provider_ready(
        object(),
        company_id=1,
        agent=agent,
    ) is True



def test_managed_readiness_remains_strict_for_profile_knowledge_and_channel(
    self_service_database,
):
    factory = self_service_database
    now = datetime.utcnow()

    with factory() as db:
        db.add(
            Company(
                id=2,
                name="Managed",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="managed",
            )
        )
        db.flush()
        db.add_all(
            [
                CompanyModule(company_id=2, module_name="ai_agent", enabled=True),
                CompanyModule(company_id=2, module_name="knowledge", enabled=True),
                CompanyModule(company_id=2, module_name="tools", enabled=True),
                ServiceSubscription(
                    company_id=2,
                    service_code="ai_agents",
                    plan_id=1,
                    status="active",
                    current_period_start=now - timedelta(days=1),
                    current_period_end=now + timedelta(days=30),
                ),
                AIAgent(
                    id=2,
                    company_id=2,
                    name="Managed employee",
                    description="Managed employee",
                    system_prompt="Managed prompt",
                    provider="mock",
                    model="mock",
                    enabled=False,
                ),
            ]
        )
        db.flush()
        db.add(
            AIAgentProfile(
                company_id=2,
                agent_id=2,
                business_name="Managed",
                business_type="business",
                reply_language="auto",
                conversation_style="professional_friendly",
            )
        )
        db.commit()

    with factory() as db:
        result = readiness.company_readiness(db, 2)

    assert result["delivery_mode"] == "managed"
    assert result["setup_ready"] is False
    assert any("Company profile is incomplete" in item for item in result["issues"])
    employee = result["agents"][0]
    assert "No enabled knowledge connected" in employee["issues"]
    assert "No channel configured" in employee["issues"]
