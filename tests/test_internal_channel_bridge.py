from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register all model metadata
from backend.app.api import internal_channel_bridge as bridge
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIMessage
from backend.app.modules.channels.models import AgentChannel


@pytest.fixture
def channel_bridge_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(bridge, "SessionLocal", factory)
    monkeypatch.setattr(bridge.settings, "N8N_SHARED_SECRET", "bridge-secret")

    with factory() as db:
        db.add(
            Company(
                id=1,
                name="Managed Customer",
                onboarding_source="managed",
                active=True,
                lifecycle_status="live",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Reception",
                description="Reply to customers.",
                system_prompt="test",
                provider="mock",
                model="mock",
                enabled=True,
            )
        )
        db.flush()
        db.add(
            AgentChannel(
                id=7,
                company_id=1,
                agent_id=1,
                channel_type="instagram",
                config={"provisioning_state": "connected"},
                enabled=True,
            )
        )
        db.commit()

    calls = []

    def fake_chat(**kwargs):
        db = kwargs["db"]
        conversation_id = kwargs.get("conversation_id")
        if conversation_id is None:
            conversation = AIConversation(
                company_id=kwargs["company_id"],
                agent_id=kwargs["agent_id"],
                channel_id=kwargs["channel_id"],
                channel_type=kwargs["channel_type"],
                external_contact_id=kwargs["external_contact_id"],
                title=kwargs["message"][:200],
            )
            db.add(conversation)
            db.flush()
        else:
            conversation = db.get(AIConversation, conversation_id)

        user = AIMessage(
            conversation_id=conversation.id,
            role="user",
            content=kwargs["message"],
            source_key=kwargs["user_message_source_key"],
        )
        assistant = AIMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="أهلًا، كيف أقدر أساعدك؟",
        )
        db.add_all([user, assistant])
        db.flush()
        db.commit()
        calls.append(dict(kwargs))
        return {
            "conversation_id": conversation.id,
            "message": {"id": user.id, "content": user.content},
            "response": {"id": assistant.id, "content": assistant.content},
        }

    monkeypatch.setattr(bridge.agent_runtime, "chat", fake_chat)
    yield factory, calls
    engine.dispose()


def test_normalized_inbound_uses_same_employee_and_deduplicates_provider_retry(
    channel_bridge_database,
):
    _factory, calls = channel_bridge_database
    payload = bridge.NormalizedChannelInbound(
        channel_id=7,
        external_contact_id="ig-user-44",
        external_message_id="ig-msg-100",
        message="مرحبا",
    )

    first = bridge.receive_normalized_channel_message(
        payload,
        x_xvond_n8n_secret="bridge-secret",
    )
    second = bridge.receive_normalized_channel_message(
        payload,
        x_xvond_n8n_secret="bridge-secret",
    )

    assert first["status"] == "reply_ready"
    assert first["channel_type"] == "instagram"
    assert first["response"]["content"] == "أهلًا، كيف أقدر أساعدك؟"
    assert second["status"] == "duplicate"
    assert second["conversation_id"] == first["conversation_id"]
    assert second["response"]["id"] == first["response"]["id"]
    assert len(calls) == 1
    assert calls[0]["channel_type"] == "instagram"
    assert calls[0]["channel_id"] == 7
    assert calls[0]["external_contact_id"] == "ig-user-44"
    assert calls[0]["user_message_source_key"] == "managed-channel:7:ig-msg-100"


def test_delivery_confirmation_records_real_roundtrip_evidence(channel_bridge_database):
    factory, _calls = channel_bridge_database
    incoming = bridge.receive_normalized_channel_message(
        bridge.NormalizedChannelInbound(
            channel_id=7,
            external_contact_id="ig-user-44",
            external_message_id="ig-msg-101",
            message="مرحبا مرة ثانية",
        ),
        x_xvond_n8n_secret="bridge-secret",
    )

    result = bridge.confirm_managed_channel_delivery(
        bridge.ChannelDeliveryConfirmation(
            channel_id=7,
            conversation_id=incoming["conversation_id"],
            response_message_id=incoming["response"]["id"],
            provider_message_id="provider-55",
        ),
        x_xvond_n8n_secret="bridge-secret",
    )

    assert result["status"] == "verified"
    with factory() as db:
        channel = db.get(AgentChannel, 7)
        assert channel.customer_roundtrip_verified_at is not None
        assert channel.customer_roundtrip_source == "n8n:instagram"


def test_channel_bridge_rejects_invalid_shared_secret(channel_bridge_database):
    with pytest.raises(HTTPException) as exc:
        bridge.receive_normalized_channel_message(
            bridge.NormalizedChannelInbound(
                channel_id=7,
                external_contact_id="ig-user",
                external_message_id="ig-msg",
                message="hello",
            ),
            x_xvond_n8n_secret="wrong",
        )
    assert exc.value.status_code == 401
