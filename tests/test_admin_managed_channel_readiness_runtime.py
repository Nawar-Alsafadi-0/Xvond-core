from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import admin_delivery_readiness as readiness
from backend.app.core.database.base import Base
from backend.app.modules.channels.acceptance import mark_customer_roundtrip
from backend.app.modules.channels.models import AgentChannel


def test_managed_channel_readiness_requires_verified_route(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, autoflush=False) as db:
            channel = AgentChannel(
                id=5,
                company_id=1,
                agent_id=2,
                channel_type="instagram",
                config={"provisioning_state": "requested"},
                enabled=False,
            )
            db.add(channel)
            db.commit()

            state = readiness._channel_state(db, 1, 2)
            assert state["configured"] is False
            assert state["live"] is False

            channel.config = {"provisioning_state": "connected"}
            db.commit()

            monkeypatch.setattr(readiness.n8n_channel_gateway, "configured", lambda: True)
            monkeypatch.setattr(
                readiness.n8n_channel_gateway,
                "check_channel",
                lambda **kwargs: {
                    "success": True,
                    "data": {"connected": True},
                },
            )

            state = readiness._channel_state(db, 1, 2)
            assert state["configured"] is True
            assert state["live"] is False

            channel.enabled = True
            mark_customer_roundtrip(channel, source="n8n:instagram")
            db.commit()

            state = readiness._channel_state(db, 1, 2)
            assert state["live"] is True
            assert state["customer_ready"] is True
            assert state["customer_ready_count"] == 1
    finally:
        engine.dispose()


def test_managed_channel_readiness_fails_closed_when_route_is_down(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, autoflush=False) as db:
            db.add(
                AgentChannel(
                    id=6,
                    company_id=1,
                    agent_id=2,
                    channel_type="telegram",
                    config={"provisioning_state": "connected"},
                    enabled=True,
                )
            )
            db.commit()

            monkeypatch.setattr(readiness.n8n_channel_gateway, "configured", lambda: True)
            monkeypatch.setattr(
                readiness.n8n_channel_gateway,
                "check_channel",
                lambda **kwargs: {
                    "success": False,
                    "error_code": "provider_check_failed",
                    "data": None,
                },
            )

            state = readiness._channel_state(db, 1, 2)
            assert state["configured"] is True
            assert state["live"] is False
            assert state["customer_ready"] is False
    finally:
        engine.dispose()
