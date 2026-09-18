from __future__ import annotations

from datetime import datetime
import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.modules.ai_agent.models import AIConversation, AIMessage
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    canonical_channel_type,
    get_channel_capability,
)
from backend.app.modules.channels.conversation_source import bind_conversation_source
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.tools.business_models import HumanHandoff


router = APIRouter(prefix="/internal/channels", tags=["Xvond Internal Channels"])
ACTIVE_HANDOFF_STATUSES = {"pending", "in_progress"}
N8N_CHANNEL_ADAPTER = "n8n_channel_bridge"


class NormalizedChannelInbound(BaseModel):
    channel_id: int
    external_contact_id: str = Field(min_length=1, max_length=200)
    external_message_id: str = Field(min_length=1, max_length=220)
    message: str = Field(min_length=1, max_length=12000)


class ChannelDeliveryConfirmation(BaseModel):
    channel_id: int
    conversation_id: int
    response_message_id: int
    provider_message_id: str | None = Field(default=None, max_length=220)


def _require_n8n_secret(value: str | None) -> None:
    expected = str(settings.N8N_SHARED_SECRET or "")
    received = str(value or "")
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Unauthorized channel workflow request")


def _bridge_channel(db, channel_id: int, *, enabled: bool = True) -> AgentChannel:
    query = db.query(AgentChannel).filter(AgentChannel.id == channel_id)
    if enabled:
        query = query.filter(AgentChannel.enabled.is_(True))
    channel = query.first()
    if channel is None:
        raise HTTPException(404, "Managed channel not found")

    channel_type = canonical_channel_type(channel.channel_type)
    capability = get_channel_capability(channel_type) or {}
    if (
        capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
        or capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
    ):
        raise HTTPException(409, "Channel is not routed through the Xvond managed channel bridge")
    return channel


def _conversation_for_contact(
    db,
    *,
    channel: AgentChannel,
    external_contact_id: str,
) -> AIConversation | None:
    return (
        db.query(AIConversation)
        .filter(
            AIConversation.company_id == channel.company_id,
            AIConversation.agent_id == channel.agent_id,
            AIConversation.channel_id == channel.id,
            AIConversation.channel_type == canonical_channel_type(channel.channel_type),
            AIConversation.external_contact_id == external_contact_id,
        )
        .order_by(AIConversation.id.desc())
        .first()
    )


def _active_handoff(db, *, company_id: int, conversation_id: int) -> HumanHandoff | None:
    return (
        db.query(HumanHandoff)
        .filter(
            HumanHandoff.company_id == company_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
        )
        .order_by(HumanHandoff.id.desc())
        .first()
    )


def _existing_reply(db, inbound: AIMessage) -> AIMessage | None:
    return (
        db.query(AIMessage)
        .filter(
            AIMessage.conversation_id == inbound.conversation_id,
            AIMessage.id > inbound.id,
            AIMessage.role == "assistant",
        )
        .order_by(AIMessage.id.asc())
        .first()
    )


@router.post("/inbound")
def receive_normalized_channel_message(
    payload: NormalizedChannelInbound,
    x_xvond_n8n_secret: str | None = Header(default=None),
):
    """Receive one provider-normalized message from an Xvond-managed n8n workflow."""

    _require_n8n_secret(x_xvond_n8n_secret)
    contact = payload.external_contact_id.strip()
    message_text = payload.message.strip()
    source_key = f"managed-channel:{payload.channel_id}:{payload.external_message_id.strip()}"
    if len(source_key) > 320:
        raise HTTPException(400, "External message identity is too long")

    db = SessionLocal()
    try:
        channel = _bridge_channel(db, payload.channel_id, enabled=True)
        channel_type = canonical_channel_type(channel.channel_type)

        existing_inbound = (
            db.query(AIMessage)
            .filter(AIMessage.source_key == source_key)
            .first()
        )
        conversation = None
        if existing_inbound is not None:
            conversation = db.get(AIConversation, existing_inbound.conversation_id)
            if (
                conversation is None
                or conversation.company_id != channel.company_id
                or conversation.agent_id != channel.agent_id
                or conversation.channel_id != channel.id
                or conversation.external_contact_id != contact
            ):
                raise HTTPException(409, "External message identity belongs to another channel conversation")
            if str(existing_inbound.content or "").strip() != message_text:
                raise HTTPException(409, "External message identity is already bound to different content")
            prior_reply = _existing_reply(db, existing_inbound)
            if prior_reply is not None:
                return {
                    "status": "duplicate",
                    "channel_id": channel.id,
                    "channel_type": channel_type,
                    "conversation_id": conversation.id,
                    "inbound_message_id": existing_inbound.id,
                    "response": {
                        "id": prior_reply.id,
                        "content": prior_reply.content,
                    },
                }
        else:
            conversation = _conversation_for_contact(
                db,
                channel=channel,
                external_contact_id=contact,
            )

        if conversation is not None:
            handoff = _active_handoff(
                db,
                company_id=channel.company_id,
                conversation_id=conversation.id,
            )
            if handoff is not None:
                if existing_inbound is None:
                    inbound = AIMessage(
                        conversation_id=conversation.id,
                        role="user",
                        content=message_text,
                        source_key=source_key,
                    )
                    db.add(inbound)
                    db.flush()
                else:
                    inbound = existing_inbound
                audit_service.log(
                    db=db,
                    company_id=channel.company_id,
                    action="channel_bridge.inbound_human_control",
                    resource_type="channel",
                    resource_id=channel.id,
                    details={
                        "conversation_id": conversation.id,
                        "inbound_message_id": inbound.id,
                        "channel_type": channel_type,
                    },
                )
                db.commit()
                return {
                    "status": "human_active",
                    "channel_id": channel.id,
                    "channel_type": channel_type,
                    "conversation_id": conversation.id,
                    "inbound_message_id": inbound.id,
                    "response": None,
                }

        result = agent_runtime.chat(
            db=db,
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            message=message_text,
            conversation_id=conversation.id if conversation is not None else None,
            channel_type=channel_type,
            channel_id=channel.id,
            external_contact_id=contact,
            user_message_source_key=source_key,
        )
        audit_service.log(
            db=db,
            company_id=channel.company_id,
            action="channel_bridge.ai_reply_created",
            resource_type="channel",
            resource_id=channel.id,
            details={
                "conversation_id": result["conversation_id"],
                "inbound_message_id": result["message"]["id"],
                "response_message_id": result["response"]["id"],
                "channel_type": channel_type,
            },
        )
        db.commit()
        return {
            "status": "reply_ready",
            "channel_id": channel.id,
            "channel_type": channel_type,
            "conversation_id": result["conversation_id"],
            "inbound_message_id": result["message"]["id"],
            "response": {
                "id": result["response"]["id"],
                "content": result["response"]["content"],
            },
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/delivery-confirmed")
def confirm_managed_channel_delivery(
    payload: ChannelDeliveryConfirmation,
    x_xvond_n8n_secret: str | None = Header(default=None),
):
    """Record real provider delivery evidence after the n8n channel workflow sends a reply."""

    _require_n8n_secret(x_xvond_n8n_secret)
    db = SessionLocal()
    try:
        channel = _bridge_channel(db, payload.channel_id, enabled=True)
        conversation = (
            db.query(AIConversation)
            .filter(
                AIConversation.id == payload.conversation_id,
                AIConversation.company_id == channel.company_id,
                AIConversation.agent_id == channel.agent_id,
                AIConversation.channel_id == channel.id,
            )
            .first()
        )
        if conversation is None:
            raise HTTPException(404, "Channel conversation not found")
        response = (
            db.query(AIMessage)
            .filter(
                AIMessage.id == payload.response_message_id,
                AIMessage.conversation_id == conversation.id,
                AIMessage.role.in_(("assistant", "human")),
            )
            .first()
        )
        if response is None:
            raise HTTPException(404, "Channel response message not found")

        verified_at = datetime.utcnow()
        channel.customer_roundtrip_verified_at = verified_at
        channel.customer_roundtrip_source = f"n8n:{canonical_channel_type(channel.channel_type)}"
        audit_service.log(
            db=db,
            company_id=channel.company_id,
            action="channel_bridge.delivery_confirmed",
            resource_type="channel",
            resource_id=channel.id,
            details={
                "conversation_id": conversation.id,
                "response_message_id": response.id,
                "provider_message_id": payload.provider_message_id,
                "channel_type": canonical_channel_type(channel.channel_type),
            },
        )
        db.commit()
        return {
            "status": "verified",
            "channel_id": channel.id,
            "conversation_id": conversation.id,
            "response_message_id": response.id,
            "verified_at": verified_at,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
