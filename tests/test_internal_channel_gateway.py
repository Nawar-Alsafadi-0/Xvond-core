from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import internal_channel_gateway as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIMessage
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.tools.business_models import HumanHandoff


@pytest.fixture
def channel_gateway_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)
    monkeypatch.setattr(api.settings, "N8N_SHARED_SECRET", "test-channel-secret")
    monkeypatch.setattr(api.audit_service, "log", lambda *args, **kwargs: None)

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Gateway Company",
                active=True,
                lifecycle_status="live",
                onboarding_source="managed",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Gateway Employee",
                description="Reply on Telegram",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentChannel(
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
        )
        db.commit()

    yield factory
    engine.dispose()


def _payload(message_id="msg-1", message="hello"):
    return api.InternalChannelMessage(
        company_id=1,
        agent_id=1,
        channel_id=10,
        channel_type="telegram",
        external_contact_id="chat-55",
        external_message_id=message_id,
        message=message,
    )


def test_channel_gateway_runs_same_employee_and_deduplicates_provider_retries(
    channel_gateway_database,
    monkeypatch,
):
    calls = []

    def fake_chat(**kwargs):
        db = kwargs["db"]
        calls.append(kwargs["message"])
        conversation = (
            db.get(AIConversation, kwargs["conversation_id"])
            if kwargs["conversation_id"] is not None
            else None
        )
        if conversation is None:
            conversation = AIConversation(
                company_id=kwargs["company_id"],
                agent_id=kwargs["agent_id"],
                channel_id=kwargs["channel_id"],
                channel_type=kwargs["channel_type"],
                external_contact_id=kwargs["external_contact_id"],
                title=kwargs["message"],
            )
            db.add(conversation)
            db.flush()
        user = (
            db.query(AIMessage)
            .filter(AIMessage.source_key == kwargs["user_message_source_key"])
            .first()
        )
        if user is None:
            user = AIMessage(
                conversation_id=conversation.id,
                role="user",
                content=kwargs["message"],
                source_key=kwargs["user_message_source_key"],
            )
            db.add(user)
            db.flush()
        reply = AIMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=f"reply:{kwargs['message']}",
        )
        db.add(reply)
        db.commit()
        return {
            "conversation_id": conversation.id,
            "response": {"content": reply.content},
        }

    monkeypatch.setattr(api.agent_runtime, "chat", fake_chat)

    first = api.receive_channel_message(
        _payload(),
        x_xvond_n8n_secret="test-channel-secret",
    )
    second = api.receive_channel_message(
        _payload(),
        x_xvond_n8n_secret="test-channel-secret",
    )

    assert first["mode"] == "ai"
    assert first["reply"] == "reply:hello"
    assert first["connection_key"] == "telegram-main"
    assert second["duplicate"] is True
    assert second["reply"] == "reply:hello"
    assert calls == ["hello"]

    with channel_gateway_database() as db:
        conversation = db.query(AIConversation).one()
        assert conversation.channel_id == 10
        assert conversation.channel_type == "telegram"
        assert conversation.external_contact_id == "chat-55"


def test_channel_gateway_stores_inbound_message_without_ai_during_handoff(
    channel_gateway_database,
    monkeypatch,
):
    with channel_gateway_database() as db:
        conversation = AIConversation(
            company_id=1,
            agent_id=1,
            channel_id=10,
            channel_type="telegram",
            external_contact_id="chat-55",
            title="Existing",
        )
        db.add(conversation)
        db.flush()
        db.add(
            HumanHandoff(
                company_id=1,
                agent_id=1,
                conversation_id=conversation.id,
                status="in_progress",
            )
        )
        db.commit()

    monkeypatch.setattr(
        api.agent_runtime,
        "chat",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("AI must stay paused")),
    )

    result = api.receive_channel_message(
        _payload(message_id="msg-human", message="need a person"),
        x_xvond_n8n_secret="test-channel-secret",
    )

    assert result["mode"] == "human"
    assert result["reply"] is None

    with channel_gateway_database() as db:
        saved = (
            db.query(AIMessage)
            .filter(AIMessage.source_key == "xvond-channel:10:msg-human")
            .one()
        )
        assert saved.role == "user"
        assert saved.content == "need a person"


def test_channel_gateway_requires_internal_shared_secret(channel_gateway_database):
    with pytest.raises(HTTPException) as exc:
        api.receive_channel_message(
            _payload(),
            x_xvond_n8n_secret="wrong-secret",
        )

    assert exc.value.status_code == 401
