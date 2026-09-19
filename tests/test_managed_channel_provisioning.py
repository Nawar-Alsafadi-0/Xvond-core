from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register metadata
from backend.app.api import admin_channels as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.channels.models import AgentChannel


ADMIN_UI = Path("frontend/admin/company-control-center.js").read_text(encoding="utf-8")


@pytest.fixture
def managed_channel_db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    monkeypatch.setattr(api.audit_service, "log", lambda *args, **kwargs: None)

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
                id=10,
                company_id=1,
                name="Receptionist",
                description="Reply on Telegram",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add_all(
            [
                AgentChannel(
                    id=100,
                    company_id=1,
                    agent_id=10,
                    channel_type="telegram",
                    config={
                        "provisioning_state": "requested",
                        "request_source": "compiled_employee_contract",
                    },
                    enabled=False,
                ),
                AgentChannel(
                    id=101,
                    company_id=1,
                    agent_id=10,
                    channel_type="instagram",
                    config={
                        "provisioning_state": "connected",
                        "connection_key": "instagram-main",
                    },
                    enabled=False,
                ),
            ]
        )
        db.commit()

    yield factory
    engine.dispose()


def test_global_managed_request_queue_returns_only_unresolved_requests(managed_channel_db):
    result = api.managed_channel_requests(SimpleNamespace(id=99, role="xvond_admin"))

    assert result["count"] == 1
    assert result["requests"][0]["channel_id"] == 100
    assert result["requests"][0]["company_name"] == "Clinic"
    assert result["requests"][0]["agent_name"] == "Receptionist"
    assert result["requests"][0]["channel_type"] == "telegram"
    assert result["requests"][0]["provisioning_state"] == "requested"


def test_managed_connect_verifies_gateway_before_marking_connected(
    managed_channel_db,
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(api.n8n_gateway, "configured", lambda: True)

    def fake_execute(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "data": {"configured": True},
            "request_id": "check-1",
            "action": "channel.check",
        }

    monkeypatch.setattr(api.n8n_gateway, "execute", fake_execute)
    monkeypatch.setattr(api, "_activation_blockers", lambda db, channel: [])

    result = api.connect_managed_channel(
        100,
        api.ManagedChannelConnect(
            connection_key="telegram-clinic",
            provider_account_label="Clinic Telegram",
            channel_instructions="Reply only to inbound customer messages.",
        ),
        SimpleNamespace(id=99, role="xvond_admin"),
    )

    assert result["status"] == "connected"
    assert result["gateway_verified"] is True
    assert result["ready"] is True
    assert result["enabled"] is False
    assert len(calls) == 1
    assert calls[0]["action"] == "channel.check"
    assert calls[0]["data"]["connection_key"] == "telegram-clinic"
    assert calls[0]["data"]["channel_type"] == "telegram"

    with managed_channel_db() as db:
        row = db.get(AgentChannel, 100)
        assert row.enabled is False
        assert row.config["provisioning_state"] == "connected"
        assert row.config["connection_key"] == "telegram-clinic"
        assert row.config["provider_account_label"] == "Clinic Telegram"
        assert row.config["connection_method"] == "xvond_managed_gateway"
        assert row.config["provisioning_verified_at"]


def test_managed_connect_fails_closed_when_provider_route_is_not_configured(
    managed_channel_db,
    monkeypatch,
):
    monkeypatch.setattr(api.n8n_gateway, "configured", lambda: True)
    monkeypatch.setattr(
        api.n8n_gateway,
        "execute",
        lambda **kwargs: {
            "success": True,
            "data": {"configured": False},
            "request_id": "check-2",
            "action": "channel.check",
        },
    )

    with pytest.raises(HTTPException) as exc:
        api.connect_managed_channel(
            100,
            api.ManagedChannelConnect(connection_key="missing-route"),
            SimpleNamespace(id=99, role="xvond_admin"),
        )

    assert exc.value.status_code == 409

    with managed_channel_db() as db:
        row = db.get(AgentChannel, 100)
        assert row.config["provisioning_state"] == "requested"
        assert "connection_key" not in row.config


def test_managed_connect_rejects_native_or_non_gateway_channels(
    managed_channel_db,
):
    with managed_channel_db() as db:
        db.add(
            AgentChannel(
                id=102,
                company_id=1,
                agent_id=10,
                channel_type="website",
                config={"allowed_domain": "example.com"},
                enabled=False,
            )
        )
        db.commit()

    with pytest.raises(HTTPException) as exc:
        api.connect_managed_channel(
            102,
            api.ManagedChannelConnect(connection_key="not-applicable"),
            SimpleNamespace(id=99, role="xvond_admin"),
        )

    assert exc.value.status_code == 409


def test_admin_ui_can_complete_managed_requests_without_exposing_provider_credentials():
    assert "Complete Setup" in ADMIN_UI
    assert "Verify & Complete Setup" in ADMIN_UI
    assert "/managed-connect" in ADMIN_UI
    assert "connection_key" in ADMIN_UI
    assert "xvondSupportMode()" in ADMIN_UI
    assert "provider credentials stay" in ADMIN_UI.lower()
    assert "access_token" not in ADMIN_UI[ADMIN_UI.index("window.openManagedChannelSetup"):ADMIN_UI.index("window.activateManagedChannel")]
