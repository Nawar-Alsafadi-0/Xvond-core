from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.api import customer_employee_builder as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.integrations.models import CompanyIntegration


USER = SimpleNamespace(id=10, company_id=1)


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            )
        )
        db.flush()
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Booking employee",
                provider="mock",
                model="mock",
                system_prompt="Book appointments",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=1,
                agent_type="employee",
                capabilities={},
                settings={
                    "employee_builder": {
                        "source_description": "Book appointments using my calendar.",
                        "compiled_at": "live-build",
                        "compiled_spec": {
                            "requirements": [],
                        },
                        "pending_revision": {
                            "status": "built",
                            "compiled_at": "pending-build",
                            "compiled_spec": {
                                "requirements": [
                                    {
                                        "key": "booking",
                                        "kind": "tool",
                                        "purpose": "Book appointments",
                                        "status": "connection_required",
                                        "requires_connection": True,
                                        "fulfillment_mode": "external_connection",
                                    }
                                ],
                                "setup_required": ["booking"],
                            },
                        },
                    }
                },
            )
        )
        db.add_all(
            [
                CompanyIntegration(
                    id=11,
                    company_id=1,
                    integration_type="calendar",
                    name="Primary Google Calendar",
                    config={
                        "provider": "google",
                        "calendar_id": "primary",
                        "timezone": "Asia/Muscat",
                        "slot_minutes": 30,
                        "access_token": "secret",
                        "_xvond_validation": {
                            "validated": True,
                            "validated_at": "2026-09-19T00:00:00Z",
                        },
                    },
                    enabled=True,
                ),
                CompanyIntegration(
                    id=12,
                    company_id=1,
                    integration_type="custom_api",
                    name="Booking API",
                    config={
                        "base_url": "https://booking.example.com",
                        "validation_endpoint": "/me",
                        "api_key": "secret",
                        "_xvond_validation": {
                            "validated": True,
                            "validated_at": "2026-09-19T00:00:00Z",
                        },
                    },
                    enabled=True,
                ),
            ]
        )
        db.commit()

    yield factory
    engine.dispose()


def _pending_requirement(factory):
    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=1).one()
        return config.settings["employee_builder"]["pending_revision"]["compiled_spec"][
            "requirements"
        ][0]


def test_calendar_booking_binds_without_customer_defined_endpoints(database):
    result = api.bind_self_service_integration(
        1,
        "booking",
        api.EmployeeBuilderIntegrationBindRequest(integration_id=11),
        USER,
    )

    assert result["status"] == "connected_to_pending_revision"
    assert result["integration"]["type"] == "calendar"

    requirement = _pending_requirement(database)
    assert requirement["integration_id"] == 11
    assert requirement["integration_type"] == "calendar"
    assert requirement["status"] == "xvond_build"
    assert requirement["integration_operations"] == {
        "availability": {"adapter": "google_calendar"},
        "execute": {"adapter": "google_calendar"},
        "reschedule": {"adapter": "google_calendar"},
        "cancel": {"adapter": "google_calendar"},
    }


def test_custom_api_booking_still_requires_real_operation_endpoints(database):
    with pytest.raises(HTTPException) as exc:
        api.bind_self_service_integration(
            1,
            "booking",
            api.EmployeeBuilderIntegrationBindRequest(integration_id=12),
            USER,
        )

    assert exc.value.status_code == 400
    assert "Required integration endpoint" in str(exc.value.detail)


def test_webhook_is_still_rejected_for_two_way_booking(database):
    with database() as db:
        db.add(
            CompanyIntegration(
                id=13,
                company_id=1,
                integration_type="webhook",
                name="One-way webhook",
                config={
                    "url": "https://hooks.example.com/booking",
                    "_xvond_validation": {
                        "validated": True,
                        "validated_at": "2026-09-19T00:00:00Z",
                    },
                },
                enabled=True,
            )
        )
        db.commit()

    with pytest.raises(HTTPException) as exc:
        api.bind_self_service_integration(
            1,
            "booking",
            api.EmployeeBuilderIntegrationBindRequest(integration_id=13),
            USER,
        )

    assert exc.value.status_code == 409
    assert "two-way API" in str(exc.value.detail)
