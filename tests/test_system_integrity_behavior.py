"""Database-backed regressions for channel/control and saved company facts."""
import hashlib
import hmac
import json
from datetime import timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.app.main import app  # Register all model metadata.
from backend.app.api import customer_inbox as inbox, customer_portal as portal_api, whatsapp_webhook as webhook
from backend.app.api import admin_company_profile as profile_api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.models.company_profile import CompanyProfile
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIMessage
from backend.app.modules.channels import whatsapp_delivery
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.conversation_source import bind_conversation_source
from backend.app.modules.channels.whatsapp_models import (
    WhatsAppInboundMessage,
    WhatsAppOutboundDelivery,
    WhatsAppSession,
)
from backend.app.modules.knowledge.models import KnowledgeDocument
from backend.app.modules.knowledge.embeddings import KnowledgeEmbeddingClient
from backend.app.modules.knowledge.service import knowledge_service


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def connect(connection, _record):
        connection.isolation_level = None
        connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    for module in (webhook, inbox, portal_api, profile_api):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(webhook.whatsapp_job_queue, "client", None)
    with factory() as db:
        db.add_all([
            Company(id=1, name="Saved Business", active=True),
            Company(id=2, name="Other Tenant", active=True),
        ])
        db.flush()
        db.add_all([
            AIAgent(id=1, company_id=1, name="Employee", system_prompt="base", provider="mock", model="mock"),
            AIAgent(id=2, company_id=2, name="Other", system_prompt="base", provider="mock", model="mock"),
        ])
        db.add_all([
            User(id=1, company_id=1, full_name="Owner", email="owner@example.test", password_hash="unused", role="owner", active=True),
            User(id=2, company_id=1, full_name="Staff", email="staff@example.test", password_hash="unused", role="employee", active=True),
        ])
        db.flush()
        db.add_all([
            AgentChannel(
                id=1,
                company_id=1,
                agent_id=1,
                channel_type="whatsapp",
                enabled=True,
                config={
                    "phone_number_id": "phone-1",
                    "app_secret": "test-signing-secret",
                    "access_token": "test-send-secret",
                    "graph_api_version": "v26.0",
                },
            ),
            AgentChannel(id=2, company_id=2, agent_id=2, channel_type="whatsapp", config={"phone_number_id": "phone-2"}),
        ])
        db.commit()

    monkeypatch.setattr(
        webhook,
        "find_channel_by_phone_number_id",
        lambda db, phone: db.get(AgentChannel, 1) if phone == "phone-1" else None,
    )
    monkeypatch.setattr(
        webhook.agent_runtime,
        "get_agent",
        lambda db, company, agent: db.get(AIAgent, agent),
    )

    def chat(db, message, conversation_id, **kwargs):
        user_message = AIMessage(conversation_id=conversation_id, role="user", content=message)
        assistant_message = AIMessage(conversation_id=conversation_id, role="assistant", content="Verified reply")
        db.add_all([user_message, assistant_message])
        db.flush()
        return {
            "conversation_id": conversation_id,
            "response": {"id": assistant_message.id, "content": "Verified reply"},
        }

    monkeypatch.setattr(webhook.agent_runtime, "chat", chat)
    monkeypatch.setattr(
        whatsapp_delivery.whatsapp_sender,
        "send_text",
        lambda **kwargs: {
            "success": True,
            "status_code": 200,
            "provider_message_id": "sent-1",
            "delivery_certainty": "accepted",
            "retryable": False,
        },
    )
    yield factory
    engine.dispose()


def process(message_id="incoming-1", text="Hello", *, echo=False, timestamp=None, statuses=None):
    if statuses is not None:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "metadata": {"phone_number_id": "phone-1"},
                        "statuses": statuses,
                    },
                }]
            }],
        }
    else:
        message = {
            "id": message_id,
            "type": "text",
            "text": {"body": text},
            "to" if echo else "from": "contact-1",
        }
        if timestamp is not None:
            message["timestamp"] = str(timestamp)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "field": "smb_message_echoes" if echo else "messages",
                    "value": {
                        "metadata": {"phone_number_id": "phone-1"},
                        "message_echoes" if echo else "messages": [message],
                    },
                }]
            }],
        }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(
        b"test-signing-secret", body, hashlib.sha256
    ).hexdigest()
    return webhook.process_webhook_payload(body, signature)


def user(role="owner", identity=1):
    return SimpleNamespace(id=identity, company_id=1, role=role)


def test_second_message_and_echo_keep_one_employee_channel_conversation(database):
    first = process()
    second = process("incoming-2")
    conversation_id = first["processed"][0]["conversation_id"]
    assert second["processed"][0]["conversation_id"] == conversation_id
    process("manual-1", "Human response", echo=True)
    process("manual-1", "Human response", echo=True)
    process("incoming-3", "Follow-up")
    with database() as db:
        assert db.query(AIConversation).count() == 1
        conversation = db.get(AIConversation, conversation_id)
        assert (conversation.channel_id, conversation.channel_type, conversation.external_contact_id) == (1, "whatsapp", "contact-1")
        assert db.query(AIMessage).filter_by(role="human").count() == 1
        assert db.query(AIMessage).filter_by(role="assistant").count() == 2
        assert db.query(WhatsAppSession).one().automation_state == "human"


def test_delayed_old_echo_cannot_undo_explicit_return_to_ai(database):
    conversation_id = process()["processed"][0]["conversation_id"]
    inbox.take_over_conversation(conversation_id, user())
    inbox.return_conversation_to_ai(conversation_id, user())

    with database() as db:
        resumed_at = db.query(WhatsAppSession).one().ai_resumed_at
        assert resumed_at is not None
        # The database stores naive UTC; timestamp() otherwise uses the host zone.
        resumed_at = resumed_at.replace(tzinfo=timezone.utc)

    old_timestamp = int((resumed_at - timedelta(seconds=10)).timestamp())
    stale = process("manual-old", "Old delayed reply", echo=True, timestamp=old_timestamp)
    assert stale["processed"][0]["status"] == "stale_echo_mirrored"
    with database() as db:
        session = db.query(WhatsAppSession).one()
        assert session.automation_state == "ai"
        assert db.query(AIMessage).filter_by(role="human").count() == 1

    new_timestamp = int((resumed_at + timedelta(seconds=2)).timestamp())
    fresh = process("manual-new", "New human reply", echo=True, timestamp=new_timestamp)
    assert fresh["processed"][0]["status"] == "human_active"
    with database() as db:
        assert db.query(WhatsAppSession).one().automation_state == "human"
        assert db.query(AIMessage).filter_by(role="human").count() == 2


def test_delivery_failure_preserves_turn_and_retries_delivery_only(database, monkeypatch):
    monkeypatch.setattr(
        whatsapp_delivery.whatsapp_sender,
        "send_text",
        lambda **kwargs: {
            "success": False,
            "status_code": 503,
            "delivery_certainty": "rejected",
            "retryable": True,
            "error_type": "HTTPError",
        },
    )
    with pytest.raises(RuntimeError, match="retry scheduled"):
        process()
    with database() as db:
        assert db.query(WhatsAppInboundMessage).count() == 1
        assert db.query(AIMessage).count() == 2
        delivery = db.query(WhatsAppOutboundDelivery).one()
        assert delivery.status == "failed"
        assert delivery.retryable is True
        assert delivery.attempts == 1

    monkeypatch.setattr(
        whatsapp_delivery.whatsapp_sender,
        "send_text",
        lambda **kwargs: {
            "success": True,
            "status_code": 200,
            "provider_message_id": "sent-retry",
            "delivery_certainty": "accepted",
            "retryable": False,
        },
    )
    retried = process()
    assert retried["processed"][0]["status"] == "delivery_recovered"
    with database() as db:
        assert db.query(AIMessage).count() == 2
        delivery = db.query(WhatsAppOutboundDelivery).one()
        assert delivery.status == "accepted"
        assert delivery.attempts == 2


def test_ambiguous_delivery_is_never_blindly_resent(database, monkeypatch):
    calls = []
    monkeypatch.setattr(
        whatsapp_delivery.whatsapp_sender,
        "send_text",
        lambda **kwargs: calls.append(kwargs) or {
            "success": False,
            "delivery_certainty": "unknown",
            "error_type": "TimeoutError",
        },
    )
    first = process()
    assert first["processed"][0]["status"] == "delivery_unknown"
    second = process()
    assert second["processed"][0]["status"] == "delivery_unknown"
    assert len(calls) == 1


def test_meta_delivery_status_updates_durable_message_state(database):
    process()
    delivered = process(statuses=[{"id": "sent-1", "status": "delivered"}])
    assert delivered["processed"][0]["status"] == "delivered"
    read = process(statuses=[{"id": "sent-1", "status": "read"}])
    assert read["processed"][0]["status"] == "read"
    with database() as db:
        delivery = db.query(WhatsAppOutboundDelivery).one()
        assert delivery.status == "read"
        assert delivery.delivered_at is not None
        assert delivery.read_at is not None


def test_portal_claim_owner_and_idempotent_human_reply(database, monkeypatch):
    conversation_id = process()["processed"][0]["conversation_id"]
    inbox.take_over_conversation(conversation_id, user())
    with pytest.raises(HTTPException) as exc:
        inbox.take_over_conversation(conversation_id, user("employee", 2))
    assert exc.value.status_code == 409

    sent = []
    monkeypatch.setattr(
        whatsapp_delivery.whatsapp_sender,
        "send_text",
        lambda **kwargs: sent.append(kwargs) or {
            "success": True,
            "status_code": 200,
            "provider_message_id": "manual-1",
            "delivery_certainty": "accepted",
            "retryable": False,
        },
    )
    payload = inbox.HumanReply(message="Manual", client_message_id="client-1")
    first = inbox.send_human_reply(conversation_id, payload, user())
    second = inbox.send_human_reply(conversation_id, payload, user())
    assert first["status"] == "sent"
    assert second["status"] == "sent"
    assert len(sent) == 1
    with database() as db:
        assert db.query(AIMessage).filter_by(role="human").count() == 1

    inbox.return_conversation_to_ai(conversation_id, user())
    with database() as db:
        assert db.query(WhatsAppSession).one().automation_state == "ai"
        assert db.get(AIConversation, conversation_id).agent_id == 1


def test_default_inbox_excludes_test_unknown_and_other_tenants(database):
    live_id = process()["processed"][0]["conversation_id"]
    with database() as db:
        db.add_all([
            AIConversation(company_id=1, agent_id=1, channel_type="portal_test", title="Test"),
            AIConversation(company_id=1, agent_id=1, title="Unknown"),
            AIConversation(company_id=2, agent_id=2, channel_type="whatsapp", title="Private"),
        ])
        db.commit()
    assert [row["id"] for row in inbox.list_inbox(current_user=user())["conversations"]] == [live_id]
    assert len(inbox.list_inbox(channel_type="portal_test", current_user=user())["conversations"]) == 1
    assert len(inbox.list_inbox(channel_type="unknown", current_user=user())["conversations"]) == 1


def test_customer_overview_counts_only_live_inbox_conversations(database):
    process()["processed"][0]["conversation_id"]
    with database() as db:
        db.add_all([
            AIConversation(company_id=1, agent_id=1, channel_type="portal_test", title="Test"),
            AIConversation(company_id=1, agent_id=1, title="Unknown"),
        ])
        db.commit()

    manager = portal_api.overview(user())
    staff = portal_api.overview(user("employee", 2))

    assert manager["summary"]["conversations"] == 1
    assert staff["summary"]["conversations"] == 1


def test_source_binding_rejects_channel_or_contact_change(database):
    conversation_id = process()["processed"][0]["conversation_id"]
    with database() as db:
        for channel_id, contact in [(2, "contact-1"), (1, "different-contact")]:
            with pytest.raises(ValueError):
                bind_conversation_source(
                    db,
                    conversation_id=conversation_id,
                    company_id=1,
                    agent_id=1,
                    channel_type="whatsapp",
                    channel_id=channel_id,
                    external_contact_id=contact,
                )


def test_identity_edit_preserves_services_and_syncs_exact_saved_facts(database):
    with database() as db:
        db.add(
            CompanyProfile(
                company_id=1,
                services=[{"name": "Consultation", "price": "25 OMR"}],
                policies=["Appointment required"],
            )
        )
        db.commit()
    result = profile_api.update_company_profile(
        1,
        profile_api.CompanyProfileUpdate(company_name="Renamed Business"),
        user(),
    )
    assert result["services"] == [{"name": "Consultation", "price": "25 OMR"}]
    assert result["policies"] == ["Appointment required"]
    with database() as db:
        content = db.query(KnowledgeDocument).one().content
        assert "Consultation" in content and "25 OMR" in content
    result = profile_api.update_company_profile(
        1,
        profile_api.CompanyProfileUpdate(company_name="Renamed Business", services=[]),
        user(),
    )
    assert result["services"] == []
    with database() as db:
        assert "Do not invent services" in db.query(KnowledgeDocument).one().content


def test_blank_reply_and_invalid_services_are_rejected():
    with pytest.raises(ValidationError):
        inbox.HumanReply(message="  \n ")
    with pytest.raises(ValidationError):
        profile_api.CompanyProfileUpdate(company_name="Business", services=[True])


def test_embedding_timeout_keeps_saved_knowledge_and_does_not_log_secrets(database, monkeypatch, caplog):
    client = KnowledgeEmbeddingClient()
    monkeypatch.setattr(type(client), "available", property(lambda self: True))

    def fail(values):
        raise httpx.ReadTimeout("secret-token private customer text")

    monkeypatch.setattr(client, "_embed_batch", fail)
    monkeypatch.setattr("backend.app.modules.knowledge.service.knowledge_embedding_client", client)
    profile_api.update_company_profile(
        1,
        profile_api.CompanyProfileUpdate(company_name="Business", services=["Consultation"]),
        user(),
    )
    with database() as db:
        knowledge_service._live_backfill_after.clear()
        context = knowledge_service.get_agent_context(
            db, 1, 1, "What services are available?"
        )
        assert "Consultation" in context
    assert "lexical fallback" in caplog.text
    assert "secret-token" not in caplog.text and "private customer text" not in caplog.text
    client._client.close()
