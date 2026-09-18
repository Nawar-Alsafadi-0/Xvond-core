from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_employee_builder as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription


@pytest.fixture
def setup_answer_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    now = datetime.utcnow()
    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                onboarding_source="self_service",
                active=False,
                lifecycle_status="onboarding",
            )
        )
        db.add(
            ServicePlan(
                id=1,
                service_code="ai_agents",
                tier="starter",
                name="Starter",
                monthly_price=Decimal("0"),
                currency="OMR",
                limits={"agents": 1, "channels": 1},
                enabled=True,
            )
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
                name="Custom setup employee",
                description="Use the owner's required account context.",
                system_prompt="before",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                settings={
                    "employee_builder": {
                        "onboarding_source": "self_service",
                        "delivery_mode": "self_service",
                        "requested_channels": [],
                        "setup_answers": {},
                        "compiled_spec": {
                            "role": "Account assistant",
                            "scope": "personal",
                            "job_brief": "Use my account context when helping me.",
                            "summary": "Owner account helper",
                            "tasks": [],
                            "permissions": [],
                            "requirements": [
                                {
                                    "key": "account_context",
                                    "kind": "data",
                                    "status": "customer_input_required",
                                    "purpose": "Use the owner's account context",
                                }
                            ],
                            "delivery": {"provisioning_version": 1},
                        },
                    }
                },
                capabilities={},
                customer_controls={},
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_setup_answer_resolves_requirement_and_reaches_runtime_prompt(setup_answer_database):
    result = api.save_self_service_setup_answer(
        1,
        "account_context",
        api.EmployeeBuilderSetupAnswerRequest(value="Primary workspace is ACME-42"),
        SimpleNamespace(company_id=1, role="owner"),
    )

    assert result["status"] == "saved"
    assert "account_context" in result["readiness"]["resolved_requirements"]

    with setup_answer_database() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        builder = dict(config.settings["employee_builder"])
        assert builder["setup_answers"]["account_context"] == "Primary workspace is ACME-42"
        assert (
            builder["compiled_spec"]["customer_inputs"]["account_context"]
            == "Primary workspace is ACME-42"
        )
        agent = db.get(AIAgent, 1)
        assert "OWNER-PROVIDED SETUP DATA" in agent.system_prompt
        assert "Primary workspace is ACME-42" in agent.system_prompt


def test_setup_answer_rejects_non_contract_and_knowledge_shortcuts(setup_answer_database):
    with pytest.raises(HTTPException) as exc:
        api.save_self_service_setup_answer(
            1,
            "unknown_key",
            api.EmployeeBuilderSetupAnswerRequest(value="No"),
            SimpleNamespace(company_id=1, role="owner"),
        )
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        api.save_self_service_setup_answer(
            1,
            "knowledge",
            api.EmployeeBuilderSetupAnswerRequest(value="Do not bypass knowledge"),
            SimpleNamespace(company_id=1, role="owner"),
        )
    assert exc.value.status_code == 409
