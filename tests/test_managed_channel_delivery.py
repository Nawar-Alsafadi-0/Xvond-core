from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register all model metadata
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIMessage
from backend.app.modules.channels import managed_delivery
from backend.app.modules.channels.models import (
    AgentChannel,
    ManagedChannelOutboundDelivery,
)


def _database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def _seed(db):
    db.add(
        Company(
            id=1,
            name="Managed Delivery Company",
            active=True,
            lifecycle_status="live",
            onboarding_source="managed",
        )
    )
    db.add(
        AIAgent(
            id=1,
            company_id=1,
            name="Employee",
            description="Managed channel employee",
            system_prompt="test",
            provider="mock",
            model="mock",
            enabled=True,
        )
    )
    db.flush()
    channel = AgentChannel(
        id=10,
        company_id=1,
        agent_id=1,
        channel_type="telegram",
        enabled=True,
        config={
            "connection_key": "telegram-main",
            "provisioning_state": "connected",
        },
    )
    db.add(channel)
    conversation = AIConversation(
        id=20,
        company_id=1,
        agent_id=1,
        channel_id=10,
        channel_type="telegram",
        external_contact_id="chat-55",
        title="Telegram chat",
    )
    db.add(conversation)
    db.flush()
    message = AIMessage(
        id=30,
        conversation_id=20,
        role="assistant",
        content="Hello from Xvond",
    )
    db.add(message)
    db.commit()
    return channel, message


def test_managed_delivery_persists_before_single_provider_attempt(monkeypatch):
    engine = _database()
    calls = []

    monkeypatch.setattr(managed_delivery.n8n_gateway, "configured", lambda: True)

    def execute(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "request_id": kwargs["request_id"],
            "action": "channel.send",
            "data": {"provider_message_id": "tg-123"},
        }

    monkeypatch.setattr(managed_delivery.n8n_gateway, "execute", execute)

    with Session(engine, autoflush=False) as db:
        channel, message = _seed(db)
        row = managed_delivery.ensure_delivery(
            db,
            idempotency_key="delivery-1",
            company_id=1,
            agent_id=1,
            conversation_id=20,
            channel_id=10,
            message_id=message.id,
            external_contact_id="chat-55",
            inbound_external_message_id="update-1",
        )
        db.commit()
        delivery_id = row.id

        result = managed_delivery.attempt_delivery(db, delivery_id=delivery_id)

        assert result["success"] is True
        assert result["status"] == "accepted"
        assert result["provider_message_id"] == "tg-123"
        assert result["attempts"] == 1
        assert len(calls) == 1
        assert calls[0]["max_retries_override"] == 0
        assert calls[0]["request_id"] == "delivery-1"
        assert calls[0]["data"]["external_contact_id"] == "chat-55"

        db.refresh(channel)
        assert channel.customer_roundtrip_verified_at is not None
        assert channel.customer_roundtrip_source == "telegram_provider_confirmed"

    engine.dispose()


def test_ambiguous_managed_delivery_is_never_blindly_resent(monkeypatch):
    engine = _database()
    calls = {"count": 0}

    monkeypatch.setattr(managed_delivery.n8n_gateway, "configured", lambda: True)

    def ambiguous(**kwargs):
        calls["count"] += 1
        raise managed_delivery.N8NGatewayError("timeout")

    monkeypatch.setattr(managed_delivery.n8n_gateway, "execute", ambiguous)

    with Session(engine, autoflush=False) as db:
        _channel, message = _seed(db)
        row = managed_delivery.ensure_delivery(
            db,
            idempotency_key="delivery-unknown",
            company_id=1,
            agent_id=1,
            conversation_id=20,
            channel_id=10,
            message_id=message.id,
            external_contact_id="chat-55",
        )
        db.commit()

        first = managed_delivery.attempt_delivery(db, delivery_id=row.id)
        second = managed_delivery.attempt_delivery(db, delivery_id=row.id)

        assert first["unknown"] is True
        assert first["status"] == "unknown"
        assert second["unknown"] is True
        assert second["status"] == "unknown"
        assert calls["count"] == 1

        stored = db.get(ManagedChannelOutboundDelivery, row.id)
        assert stored.retryable is False
        assert stored.attempts == 1
        assert stored.last_error_code == "network_outcome_unknown"

    engine.dispose()


def test_provider_success_without_message_identity_is_not_accepted(monkeypatch):
    engine = _database()
    monkeypatch.setattr(managed_delivery.n8n_gateway, "configured", lambda: True)
    monkeypatch.setattr(
        managed_delivery.n8n_gateway,
        "execute",
        lambda **kwargs: {
            "success": True,
            "request_id": kwargs["request_id"],
            "action": "channel.send",
            "data": {},
        },
    )

    with Session(engine, autoflush=False) as db:
        channel, message = _seed(db)
        row = managed_delivery.ensure_delivery(
            db,
            idempotency_key="delivery-no-provider-id",
            company_id=1,
            agent_id=1,
            conversation_id=20,
            channel_id=10,
            message_id=message.id,
            external_contact_id="chat-55",
        )
        db.commit()

        result = managed_delivery.attempt_delivery(db, delivery_id=row.id)

        assert result["success"] is False
        assert result["unknown"] is True
        assert result["status"] == "unknown"
        assert result["error_code"] == "provider_message_id_missing"
        db.refresh(channel)
        assert channel.customer_roundtrip_verified_at is None

    engine.dispose()


def test_interrupted_sending_state_becomes_unknown_without_network_call(monkeypatch):
    engine = _database()
    calls = {"count": 0}
    monkeypatch.setattr(managed_delivery.n8n_gateway, "configured", lambda: True)
    monkeypatch.setattr(
        managed_delivery.n8n_gateway,
        "execute",
        lambda **kwargs: calls.__setitem__("count", calls["count"] + 1),
    )

    with Session(engine, autoflush=False) as db:
        _channel, message = _seed(db)
        row = ManagedChannelOutboundDelivery(
            idempotency_key="delivery-interrupted",
            company_id=1,
            agent_id=1,
            conversation_id=20,
            channel_id=10,
            message_id=message.id,
            external_contact_id="chat-55",
            status="sending",
            attempts=1,
            retryable=False,
        )
        db.add(row)
        db.commit()

        result = managed_delivery.attempt_delivery(db, delivery_id=row.id)

        assert result["unknown"] is True
        assert result["status"] == "unknown"
        assert result["error_code"] == "interrupted_after_send_started"
        assert calls["count"] == 0

    engine.dispose()
