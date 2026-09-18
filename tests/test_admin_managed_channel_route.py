from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import admin_channels as api
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.base import Base
from backend.app.modules.channels.models import AgentChannel


@pytest.fixture
def managed_channel_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        db.add(
            AgentChannel(
                id=12,
                company_id=4,
                agent_id=8,
                channel_type="instagram",
                config={
                    "provisioning_state": "requested",
                    "request_source": "job_brief",
                },
                enabled=False,
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_admin_can_verify_managed_route_without_storing_provider_credentials(
    managed_channel_database,
    monkeypatch,
):
    factory = managed_channel_database
    monkeypatch.setattr(api.n8n_channel_gateway, "configured", lambda: True)
    monkeypatch.setattr(
        api.n8n_channel_gateway,
        "check_channel",
        lambda **kwargs: {
            "success": True,
            "data": {
                "connected": True,
                "channel_id": kwargs["channel_id"],
                "channel_type": kwargs["channel_type"],
                "route_key": "4:12",
            },
        },
    )

    result = api.verify_managed_channel_route(
        12,
        SimpleNamespace(id=99),
    )

    assert result["status"] == "verified"
    assert result["configured"] is True
    assert result["connected"] is True
    assert result["runtime_adapter"] == "n8n_channel_bridge"

    with factory() as db:
        channel = db.get(AgentChannel, 12)
        config = reveal_config(channel.config)
        assert config["provisioning_state"] == "connected"
        assert config["provisioning_method"] == "xvond_managed_n8n"
        assert config["request_source"] == "job_brief"
        assert "access_token" not in config
        assert "bot_token" not in config


def test_admin_does_not_mark_managed_route_connected_when_gateway_route_is_missing(
    managed_channel_database,
    monkeypatch,
):
    factory = managed_channel_database
    monkeypatch.setattr(api.n8n_channel_gateway, "configured", lambda: True)
    monkeypatch.setattr(
        api.n8n_channel_gateway,
        "check_channel",
        lambda **kwargs: {
            "success": False,
            "error_code": "provider_not_configured",
            "data": None,
        },
    )

    with pytest.raises(HTTPException) as exc:
        api.verify_managed_channel_route(
            12,
            SimpleNamespace(id=99),
        )
    assert exc.value.status_code == 409

    with factory() as db:
        config = reveal_config(db.get(AgentChannel, 12).config)
        assert config["provisioning_state"] == "requested"


def test_admin_channel_ui_exposes_route_verification_then_normal_activation():
    source = open(
        "frontend/admin/company-control-center.js",
        encoding="utf-8",
    ).read()
    assert "Verify Xvond Route" in source
    assert "/verify-managed-route" in source
    assert "verifyManagedChannelRoute" in source
    assert "setWorkspaceChannelStatus" in source
