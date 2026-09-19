from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.api.customer_employee_builder import (
    _auto_bind_single_packaged_integrations,
)
from backend.app.core.database.base import Base
from backend.app.modules.integrations.models import CompanyIntegration


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
