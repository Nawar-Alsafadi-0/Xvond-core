from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.api import customer_employee_builder as api
from backend.app.api.customer_employee_builder import (
    _auto_bind_single_packaged_integrations,
)
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.integrations.models import CompanyIntegration


BUILDER_UI = Path("frontend/customer/employee-builder.js").read_text(encoding="utf-8")


def _calendar(integration_id: int, *, validated: bool = True) -> CompanyIntegration:
    config = {
        "provider": "google",
        "calendar_id": "primary",
        "timezone": "Asia/Muscat",
        "slot_minutes": 30,
        "access_token": f"token-{integration_id}",
    }
    if validated:
        config["_xvond_validation"] = {
            "validated": True,
            "validated_at": "2026-09-19T00:00:00Z",
        }
    return CompanyIntegration(
        id=integration_id,
        company_id=1,
        integration_type="calendar",
        name=f"Calendar {integration_id}",
        config=config,
        enabled=True,
    )


def _booking_spec() -> dict:
    return {
        "job_brief": "Book clinic appointments.",
        "scope": "business",
        "requirements": [
            {
                "key": "booking",
                "kind": "integration",
                "purpose": "Book appointments",
                "status": "connection_required",
                "delivery_mode": "connect",
            }
        ],
        "setup_required": ["booking"],
        "delivery": {"provisioning_version": 1},
    }


def test_single_validated_packaged_connector_is_auto_bound():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        db.add(_calendar(10))
        db.commit()

        result, bound = _auto_bind_single_packaged_integrations(
            db,
            company_id=1,
            spec=_booking_spec(),
        )

    requirement = result["requirements"][0]
    assert bound == ["booking"]
    assert requirement["integration_id"] == 10
    assert requirement["integration_type"] == "calendar"
    assert requirement["fulfillment_mode"] == "external_connection"
    assert requirement["validation_required"] is True
    assert requirement["requires_connection"] is True
    assert requirement["status"] == "xvond_build"
    assert requirement["delivery_mode"] == "compose"
    assert set(requirement["integration_operations"]) == {
        "availability",
        "execute",
        "cancel",
    }
    assert "booking" not in result["setup_required"]
    engine.dispose()


def test_multiple_validated_packaged_connectors_remain_owner_choice():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        db.add_all([_calendar(10), _calendar(11)])
        db.commit()

        result, bound = _auto_bind_single_packaged_integrations(
            db,
            company_id=1,
            spec=_booking_spec(),
        )

    requirement = result["requirements"][0]
    assert bound == []
    assert requirement["status"] == "connection_required"
    assert "integration_id" not in requirement
    assert result["setup_required"] == ["booking"]
    engine.dispose()


def test_unvalidated_packaged_connector_is_not_auto_bound():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        db.add(_calendar(10, validated=False))
        db.commit()

        result, bound = _auto_bind_single_packaged_integrations(
            db,
            company_id=1,
            spec=_booking_spec(),
        )

    assert bound == []
    assert result["requirements"][0]["status"] == "connection_required"
    engine.dispose()


def test_generic_endpoint_connector_is_never_auto_bound():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as db:
        db.add(
            CompanyIntegration(
                id=20,
                company_id=1,
                integration_type="custom_api",
                name="Clinic API",
                config={
                    "base_url": "https://api.example.com",
                    "validation_endpoint": "/health",
                    "_xvond_validation": {
                        "validated": True,
                        "validated_at": "2026-09-19T00:00:00Z",
                    },
                },
                enabled=True,
            )
        )
        db.commit()

        result, bound = _auto_bind_single_packaged_integrations(
            db,
            company_id=1,
            spec=_booking_spec(),
        )

    assert bound == []
    assert "integration_id" not in result["requirements"][0]
    engine.dispose()


def test_existing_binding_is_preserved_without_reselection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    spec = _booking_spec()
    spec["requirements"][0].update(
        {
            "integration_id": 77,
            "integration_type": "calendar",
            "integration_operations": {"execute": {"adapter": "google_calendar"}},
            "fulfillment_mode": "external_connection",
        }
    )
    with Session(engine, autoflush=False) as db:
        db.add(_calendar(10))
        db.commit()

        result, bound = _auto_bind_single_packaged_integrations(
            db,
            company_id=1,
            spec=spec,
        )

    assert bound == []
    assert result["requirements"][0]["integration_id"] == 77
    engine.dispose()


def test_auto_resolve_endpoint_reuses_new_connection_and_invalidates_preview(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    monkeypatch.setattr(
        api,
        "provision_compiled_capabilities",
        lambda db, *, agent_id, spec: (
            {**spec, "delivery": {"provisioning_version": 1}},
            {"provisioning_version": 1},
        ),
    )

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Clinic",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            )
        )
        db.flush()
        db.add(
            AIAgent(
                id=5,
                company_id=1,
                name="Receptionist",
                description="Book clinic appointments.",
                system_prompt="old",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=5,
                agent_type="employee",
                settings={
                    "employee_builder": {
                        "compiled_at": "2026-09-19T00:00:00Z",
                        "last_tested_at": "2026-09-19T00:01:00Z",
                        "last_tested_compiled_at": "2026-09-19T00:00:00Z",
                        "compiled_spec": _booking_spec(),
                    }
                },
                capabilities={"booking": True},
                customer_controls={},
            )
        )
        db.add(_calendar(10))
        db.commit()

    result = api.auto_resolve_self_service_integrations(
        5,
        SimpleNamespace(company_id=1),
    )

    assert result["status"] == "resolved"
    assert result["bound_requirements"] == ["booking"]

    with factory() as db:
        config = db.query(AgentConfig).filter_by(agent_id=5).one()
        builder = config.settings["employee_builder"]
        requirement = builder["compiled_spec"]["requirements"][0]
        assert requirement["integration_id"] == 10
        assert "last_tested_at" not in builder
        assert "last_tested_compiled_at" not in builder

    engine.dispose()


def test_builder_attempts_safe_auto_resolution_after_a_new_connection_exists():
    assert "hasResolvableConnectionRequirement" in BUILDER_UI
    assert "/connections/auto-resolve" in BUILDER_UI
    assert "Automatic connection reuse skipped" in BUILDER_UI
