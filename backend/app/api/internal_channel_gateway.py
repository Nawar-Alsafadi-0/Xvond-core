from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.modules.ai_agent.models import AIConversation, AIMessage
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    N8N_CHANNEL_ADAPTER,
    canonical_channel_type,
    get_channel_capability,
)
from backend.app.modules.channels.acceptance import mark_customer_roundtrip
from backend.app.modules.channels.managed_delivery import (
    attempt_delivery as attempt_managed_delivery,
    ensure_delivery as ensure_managed_delivery,
)
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.tools.business_models import HumanHandoff


router = APIRouter(prefix="/internal/channels", tags=["Xvond Internal Channels"])

ACTIVE_HANDOFF_STATUSES = {"pending", "in_progress"}


class ChannelDeliveryConfirmation(BaseModel):
    channel_id: int
    conversation_id: int
    response_message_id: int
    provider_message_id: str = Field(min_length=1, max_length=200)

    @field_validator("provider_message_id")
    @classmethod
    def clean_provider_message_id(cls, value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError("Provider message identity cannot be blank")
        return clean


class InternalChannelMessage(BaseModel):
    company_id: int
    agent_id: int
    channel_id: int
    channel_type: str = Field(min_length=1, max_length=100)
    external_contact_id: str = Field(min_length=1, max_length=200)
    external_message_id: str = Field(min_length=1, max_length=180)
    message: str = Field(min_length=1, max_length=12000)

    @field_validator(
        "channel_type",
        "external_contact_id",
        "external_message_id",
        "message",
    )
    @classmethod
    def clean_text(cls, value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError("Value cannot be blank")
        return clean


def _require_workflow_secret(value: str | None) -> None:
    expected = str(settings.N8N_SHARED_SECRET or "")
    received = str(value or "")
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Unauthorized channel workflow request")


def _active_handoff(db, *, company_id: int, conversation_id: int):
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


def _existing_conversation(
    db,
    *,
    company_id: int,
    agent_id: int,
    channel_id: int,
    external_contact_id: str,
):
    return (
        db.query(AIConversation)
        .filter(
            AIConversation.company_id == company_id,
            AIConversation.agent_id == agent_id,
            AIConversation.channel_id == channel_id,
            AIConversation.external_contact_id == external_contact_id,
        )
        .order_by(AIConversation.id.desc())
        .first()
    )


def _existing_reply(db, user_message: AIMessage):
    next_message = (
        db.query(AIMessage)
        .filter(
            AIMessage.conversation_id == user_message.conversation_id,
            AIMessage.id > user_message.id,
        )
        .order_by(AIMessage.id.asc())
        .first()
    )
    if next_message is None or next_message.role != "assistant":
        return None
    return next_message


def _channel_response(
    *,
    channel: AgentChannel,
    conversation_id: int,
    external_contact_id: str,
    external_message_id: str,
    reply: str | None,
    response_message_id: int | None = None,
    mode: str,
    duplicate: bool = False,
    delivery: dict | None = None,
) -> dict:
    config = reveal_config(channel.config) or {}
    return {
        "success": True,
        "company_id": channel.company_id,
        "agent_id": channel.agent_id,
        "channel_id": channel.id,
        "channel_type": canonical_channel_type(channel.channel_type),
        "connection_key": str(config.get("connection_key") or "").strip(),
        "conversation_id": conversation_id,
        "external_contact_id": external_contact_id,
        "external_message_id": external_message_id,
        "response_message_id": response_message_id,
        "mode": mode,
        "duplicate": duplicate,
        "reply": reply,
        "delivery": delivery,
    }


def _deliver_ai_reply(
    db,
    *,
    channel: AgentChannel,
    conversation_id: int,
    external_contact_id: str,
    external_message_id: str,
    response_message: AIMessage,
) -> dict:
    key = f"channel-reply:{channel.id}:{external_message_id}"
    delivery = ensure_managed_delivery(
        db,
        idempotency_key=key,
        company_id=channel.company_id,
        agent_id=channel.agent_id,
        conversation_id=conversation_id,
        channel_id=channel.id,
        message_id=response_message.id,
        external_contact_id=external_contact_id,
        inbound_external_message_id=external_message_id,
    )
    db.commit()
    return attempt_managed_delivery(db, delivery_id=delivery.id)


@router.post("/message")
def receive_channel_message(
    payload: InternalChannelMessage,
    x_xvond_n8n_secret: str | None = Header(default=None),
):
    """Receive one normalized external-channel message from Xvond's workflow plane.

    Provider-specific webhook parsing and credentials stay outside Core. Core owns
    channel identity, conversation continuity, AI execution, handoff state and
    persisted messages.
    """

    _require_workflow_secret(x_xvond_n8n_secret)
    channel_type = canonical_channel_type(payload.channel_type)
    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.id == payload.channel_id,
                AgentChannel.company_id == payload.company_id,
                AgentChannel.agent_id == payload.agent_id,
                AgentChannel.channel_type == channel_type,
                AgentChannel.enabled.is_(True),
            )
            .first()
        )
        if channel is None:
            raise HTTPException(404, "Active Xvond channel not found")

        capability = get_channel_capability(channel_type) or {}
        if (
            capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
            or capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
        ):
            raise HTTPException(409, "Channel is not routed through the Xvond channel gateway")

        config = reveal_config(channel.config) or {}
        if str(config.get("provisioning_state") or "").strip().lower() != "connected":
            raise HTTPException(409, "Channel provisioning is not complete")
        if not str(config.get("connection_key") or "").strip():
            raise HTTPException(409, "Channel connection key is missing")

        source_key = f"xvond-channel:{channel.id}:{payload.external_message_id}"
        existing_message = (
            db.query(AIMessage)
            .filter(AIMessage.source_key == source_key)
            .first()
        )
        conversation = None
        if existing_message is not None:
            if str(existing_message.content or "").strip() != payload.message:
                raise HTTPException(
                    409,
                    "External message identity is already bound to different content",
                )
            existing_reply = _existing_reply(db, existing_message)
            if existing_reply is not None:
                delivery_result = _deliver_ai_reply(
                    db,
                    channel=channel,
                    conversation_id=existing_message.conversation_id,
                    external_contact_id=payload.external_contact_id,
                    external_message_id=payload.external_message_id,
                    response_message=existing_reply,
                )
                return _channel_response(
                    channel=channel,
                    conversation_id=existing_message.conversation_id,
                    external_contact_id=payload.external_contact_id,
                    external_message_id=payload.external_message_id,
                    reply=existing_reply.content,
                    response_message_id=existing_reply.id,
                    mode="ai",
                    duplicate=True,
                    delivery=delivery_result,
                )
            conversation = db.get(AIConversation, existing_message.conversation_id)
            if conversation is None:
                raise HTTPException(409, "External message conversation is unavailable")
            if _active_handoff(
                db,
                company_id=payload.company_id,
                conversation_id=conversation.id,
            ) is not None:
                return _channel_response(
                    channel=channel,
                    conversation_id=conversation.id,
                    external_contact_id=payload.external_contact_id,
                    external_message_id=payload.external_message_id,
                    reply=None,
                    mode="human",
                    duplicate=True,
                )

        if conversation is None:
            conversation = _existing_conversation(
                db,
                company_id=payload.company_id,
                agent_id=payload.agent_id,
                channel_id=channel.id,
                external_contact_id=payload.external_contact_id,
            )
        if conversation is not None and _active_handoff(
            db,
            company_id=payload.company_id,
            conversation_id=conversation.id,
        ) is not None:
            message = AIMessage(
                conversation_id=conversation.id,
                role="user",
                content=payload.message,
                source_key=source_key,
            )
            db.add(message)
            audit_service.log(
                db=db,
                company_id=payload.company_id,
                action="channel.inbound_human_mode",
                resource_type="channel",
                resource_id=channel.id,
                details={
                    "agent_id": payload.agent_id,
                    "conversation_id": conversation.id,
                    "channel_type": channel_type,
                    "external_message_id": payload.external_message_id,
                },
            )
            db.commit()
            return _channel_response(
                channel=channel,
                conversation_id=conversation.id,
                external_contact_id=payload.external_contact_id,
                external_message_id=payload.external_message_id,
                reply=None,
                mode="human",
            )

        result = agent_runtime.chat(
            db=db,
            company_id=payload.company_id,
            agent_id=payload.agent_id,
            message=payload.message,
            conversation_id=conversation.id if conversation is not None else None,
            commit=True,
            allow_tools=True,
            channel_type=channel_type,
            channel_id=channel.id,
            external_contact_id=payload.external_contact_id,
            user_message_source_key=source_key,
        )
        response_data = result.get("response") or {}
        response_message = db.get(AIMessage, response_data.get("id"))
        if response_message is None:
            raise HTTPException(500, "AI response message was not persisted")
        delivery_result = _deliver_ai_reply(
            db,
            channel=channel,
            conversation_id=result["conversation_id"],
            external_contact_id=payload.external_contact_id,
            external_message_id=payload.external_message_id,
            response_message=response_message,
        )
        audit_service.log(
            db=db,
            company_id=payload.company_id,
            action="channel.inbound_ai_reply",
            resource_type="channel",
            resource_id=channel.id,
            details={
                "agent_id": payload.agent_id,
                "conversation_id": result["conversation_id"],
                "channel_type": channel_type,
                "external_message_id": payload.external_message_id,
                "delivery_id": delivery_result.get("delivery_id"),
                "delivery_status": delivery_result.get("status"),
            },
        )
        db.commit()
        return _channel_response(
            channel=channel,
            conversation_id=result["conversation_id"],
            external_contact_id=payload.external_contact_id,
            external_message_id=payload.external_message_id,
            reply=str(response_data.get("content") or ""),
            response_message_id=response_message.id,
            mode="ai",
            delivery=delivery_result,
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



@router.post("/delivery-confirmed")
def confirm_channel_delivery(
    payload: ChannelDeliveryConfirmation,
    x_xvond_n8n_secret: str | None = Header(default=None),
):
    """Persist proof that a real managed-channel reply reached its provider path."""

    _require_workflow_secret(x_xvond_n8n_secret)
    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.id == payload.channel_id,
                AgentChannel.enabled.is_(True),
            )
            .with_for_update()
            .first()
        )
        if channel is None:
            raise HTTPException(404, "Active Xvond channel not found")

        channel_type = canonical_channel_type(channel.channel_type)
        capability = get_channel_capability(channel_type) or {}
        if (
            capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
            or capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
        ):
            raise HTTPException(
                409,
                "Channel is not routed through the Xvond channel gateway",
            )

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

        response_message = (
            db.query(AIMessage)
            .filter(
                AIMessage.id == payload.response_message_id,
                AIMessage.conversation_id == conversation.id,
                AIMessage.role == "assistant",
            )
            .first()
        )
        if response_message is None:
            raise HTTPException(404, "Channel response message not found")

        first_verified = mark_customer_roundtrip(
            channel,
            source=f"xvond_managed:{channel_type}",
        )
        audit_service.log(
            db=db,
            company_id=channel.company_id,
            action="channel.delivery_confirmed",
            resource_type="channel",
            resource_id=channel.id,
            details={
                "agent_id": channel.agent_id,
                "conversation_id": conversation.id,
                "response_message_id": response_message.id,
                "provider_message_id": payload.provider_message_id,
                "channel_type": channel_type,
                "first_verified_roundtrip": first_verified,
            },
        )
        db.commit()
        return {
            "success": True,
            "status": "verified",
            "channel_id": channel.id,
            "conversation_id": conversation.id,
            "response_message_id": response_message.id,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
