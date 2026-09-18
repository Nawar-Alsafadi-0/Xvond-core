from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_meta_whatsapp as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.channels.models import AgentChannel


USER = SimpleNamespace(company_id=1, role="owner", id=10)


@pytest.fixture
def whatsapp_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        db.add_all([
            Company(
                id=1,
                name="Self Service",
                active=True,
                lifecycle_status="live",
                onboarding_source="self_service",
            ),
            Company(
                id=2,
                name="Managed",
                active=True,
                lifecycle_status="live",
                onboarding_source="managed",
            ),
        ])
        db.flush()
        db.add_all([
            AIAgent(
                id=1,
                company_id=1,
                name="Self-Service employee",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=True,
            ),
            AIAgent(
                id=2,
                company_id=2,
                name="Managed employee",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=True,
            ),
        ])
        db.commit()

    yield factory
    engine.dispose()


def _signup_data():
    return api.CustomerEmbeddedSignupComplete(
        agent_id=1,
        code="meta-code",
        waba_id="waba-1",
        phone_number_id="phone-1",
        connection_mode="embedded_signup",
    )


def _meta_config():
    return {
        "app_id": "app-1",
        "config_id": "config-1",
        "verify_token": "verify",
        "app_secret": "secret",
        "graph_api_version": "v26.0",
    }


def test_live_self_service_whatsapp_change_is_blocked_before_meta_exchange(
    whatsapp_database,
    monkeypatch,
):
    monkeypatch.setattr(api, "_ensure_meta_configured", _meta_config)

    exchanged = []
    def exchange(*args, **kwargs):
        exchanged.append(True)
        return "token"

    monkeypatch.setattr(api, "_exchange_code_for_token", exchange)

    with pytest.raises(HTTPException) as exc:
        api.complete_embedded_signup(_signup_data(), USER)

    assert exc.value.status_code == 409
    assert "Deactivate" in str(exc.value.detail)
    assert exchanged == []


def test_whatsapp_setup_rechecks_live_state_after_meta_window(
    whatsapp_database,
    monkeypatch,
):
    factory = whatsapp_database
    with factory() as db:
        db.get(AIAgent, 1).enabled = False
        db.get(Company, 1).active = False
        db.get(Company, 1).lifecycle_status = "paused"
        db.commit()

    monkeypatch.setattr(api, "_ensure_meta_configured", _meta_config)
    monkeypatch.setattr(api, "_exchange_code_for_token", lambda *args, **kwargs: "token")

    def resolve_phone(**kwargs):
        # Simulate the customer launching the employee while Meta's signup
        # window is still open. The second DB check must reject stale setup.
        with factory() as db:
            db.get(AIAgent, 1).enabled = True
            db.get(Company, 1).active = True
            db.get(Company, 1).lifecycle_status = "live"
            db.commit()
        return {
            "id": "phone-1",
            "display_phone_number": "+96800000000",
            "verified_name": "Test",
        }

    monkeypatch.setattr(api, "_resolve_signup_phone", resolve_phone)
    monkeypatch.setattr(api, "_subscribe_app_to_waba", lambda **kwargs: None)

    with pytest.raises(HTTPException) as exc:
        api.complete_embedded_signup(_signup_data(), USER)

    assert exc.value.status_code == 409
    assert "Deactivate" in str(exc.value.detail)
    with factory() as db:
        assert db.query(AgentChannel).count() == 0


def test_embedded_signup_config_marks_live_self_service_employee_read_only(
    whatsapp_database,
    monkeypatch,
):
    monkeypatch.setattr(api, "_meta_settings", _meta_config)
    monkeypatch.setattr(api, "_missing_meta_settings", lambda meta: [])
    monkeypatch.setattr(
        api,
        "whatsapp_connection_state",
        lambda config, verify_remote=False: {
            "connected": False,
            "connection_status": "not_configured",
            "connection_issue": None,
            "connection_checked_at": None,
            "meta_error_code": None,
            "coexistence_ready": False,
            "echo_received": False,
        },
    )

    result = api.embedded_signup_config(1, USER)

    assert result["ready"] is True
    assert result["can_edit"] is False


def test_live_managed_employee_keeps_existing_whatsapp_edit_behavior(
    whatsapp_database,
):
    factory = whatsapp_database
    with factory() as db:
        managed = db.get(AIAgent, 2)
        assert api._self_service_whatsapp_can_edit(db, managed) is True


def test_customer_whatsapp_ui_respects_live_edit_guard():
    from pathlib import Path

    js = Path("frontend/customer/meta-whatsapp.js").read_text(encoding="utf-8")
    assert "config.can_edit === false" in js
    assert "أوقف الموظف أولًا قبل تغيير اتصال واتساب" in js
    assert "(config.ready && config.can_edit !== false)" in js
