from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_

from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_operator
from backend.app.core.n8n_channel_gateway import N8NChannelGatewayError, n8n_channel_gateway
from backend.app.models.user import User
from backend.app.modules.ai_agent.customer_access import can_view_conversations
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIMessage
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    N8N_CHANNEL_RUNTIME_ADAPTER,
    canonical_channel_type,
    get_channel_capability,
    live_managed_channel_types,
    live_self_service_channel_types,
)
from backend.app.modules.channels.handoff import (
    activate_human_handoff,
    human_handoff_active,
    resume_ai,
)
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_delivery import (
    attempt_delivery,
    delivery_payload,
    ensure_delivery,
)
from backend.app.modules.channels.whatsapp_models import WhatsAppOutboundDelivery, WhatsAppSession
from backend.app.modules.channels.whatsapp_queue import whatsapp_job_queue
from backend.app.modules.tools.business_models import HumanHandoff

router = APIRouter(prefix="/customer/inbox", tags=["Customer Conversation Inbox"])
ACTIVE_HANDOFF_STATUSES = {"pending", "in_progress"}
HANDOFF_MANAGER_ROLES = {"owner", "admin", "manager"}
LIVE_INBOX_CHANNELS = (
    live_self_service_channel_types() | live_managed_channel_types()
) - {"xvond"}

# Handoff is a channel capability, not a generic button. Only expose controls when
# Xvond has a real delivery path back to the customer on that channel.
HANDOFF_CAPABILITIES = {
    "whatsapp": {
        "handoff_supported": True,
        "human_reply_supported": True,
        "human_reply_delivery": "whatsapp",
    },
    "website": {
        "handoff_supported": True,
        "human_reply_supported": True,
        "human_reply_delivery": "website_widget",
    },
    "voice": {
        "handoff_supported": False,
        "human_reply_supported": False,
        "human_reply_delivery": None,
    },
    "instagram": {
        "handoff_supported": False,
        "human_reply_supported": False,
        "human_reply_delivery": None,
    },
    "portal_test": {
        "handoff_supported": False,
        "human_reply_supported": False,
        "human_reply_delivery": None,
    },
}


class HumanReply(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    client_message_id: str | None = Field(default=None, max_length=120)

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A reply cannot be blank")
        return value

    @field_validator("client_message_id")
    @classmethod
    def clean_client_message_id(cls, value):
        value = str(value or "").strip()
        return value or None


def _visible_agents(db, current_user: User) -> list[AIAgent]:
    company_id = current_user.company_id
    agents = (
        db.query(AIAgent)
        .filter(AIAgent.company_id == company_id)
        .order_by(AIAgent.id.asc())
        .all()
    )
    if not agents:
        return []

    configs = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id.in_([item.id for item in agents]))
        .all()
    )
    config_by_agent = {item.agent_id: item for item in configs}
    return [item for item in agents if can_view_conversations(config_by_agent.get(item.id))]


def _channel_label(channel_type: str | None) -> str:
    value = canonical_channel_type(channel_type or "unknown")
    if value == "portal_test":
        return "Test Console"
    if value == "unknown":
        return "Unclassified"
    capability = get_channel_capability(value)
    if capability is not None:
        return str(capability.get("name") or value)
    return value.replace("_", " ").title()


def _handoff_capabilities(channel_type: str | None) -> dict:
    value = canonical_channel_type(channel_type or "unknown")
    capability = get_channel_capability(value) or {}
    if (
        capability.get("runtime_state") == CHANNEL_RUNTIME_LIVE
        and capability.get("runtime_adapter") == N8N_CHANNEL_RUNTIME_ADAPTER
    ):
        return {
            "handoff_supported": True,
            "human_reply_supported": True,
            "human_reply_delivery": "n8n_channel",
        }
    capabilities = HANDOFF_CAPABILITIES.get(value)
    if capabilities is None:
        capabilities = {
            "handoff_supported": False,
            "human_reply_supported": False,
            "human_reply_delivery": None,
        }
    return dict(capabilities)


def _session(db, company_id: int, conversation_id: int):
    return (
        db.query(WhatsAppSession)
        .filter(
            WhatsAppSession.company_id == company_id,
            WhatsAppSession.conversation_id == conversation_id,
        )
        .first()
    )


def _active_handoff(db, company_id: int, conversation_id: int):
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


def _claim_handoff(handoff: HumanHandoff, current_user: User) -> None:
    assigned_user_id = handoff.assigned_user_id
    if assigned_user_id is not None and assigned_user_id != current_user.id:
        raise HTTPException(409, "This conversation is already owned by another teammate")
    now = datetime.utcnow()
    handoff.assigned_user_id = current_user.id
    handoff.status = "in_progress"
    if handoff.taken_over_at is None:
        handoff.taken_over_at = now
    handoff.updated_at = now


def _require_handoff_owner(handoff: HumanHandoff, current_user: User) -> None:
    if handoff.assigned_user_id == current_user.id:
        return
    if handoff.assigned_user_id is None:
        raise HTTPException(409, "Claim this conversation before replying")
    raise HTTPException(409, "This conversation is owned by another teammate")


def _require_handoff_close_permission(handoff: HumanHandoff, current_user: User) -> None:
    if handoff.assigned_user_id == current_user.id:
        return
    if current_user.role in HANDOFF_MANAGER_ROLES:
        return
    if handoff.assigned_user_id is None:
        raise HTTPException(409, "Claim this conversation before returning it to AI")
    raise HTTPException(
        409,
        "Only the assigned teammate or a manager can return this conversation to AI",
    )


def _handoff_state(db, current_user: User, conversation_id: int):
    session = _session(db, current_user.company_id, conversation_id)
    handoff = _active_handoff(db, current_user.company_id, conversation_id)
    active = bool(handoff)
    if session is not None and human_handoff_active(session):
        active = True

    assigned_user_id = handoff.assigned_user_id if handoff else None
    assigned_to_me = bool(assigned_user_id is not None and assigned_user_id == current_user.id)
    manager_override = current_user.role in HANDOFF_MANAGER_ROLES
    return {
        "mode": "human" if active else "ai",
        "handoff_reason": handoff.reason if handoff else (session.handoff_reason if session else None),
        "handoff_status": handoff.status if handoff else None,
        "handoff_assigned_user_id": assigned_user_id,
        "handoff_assigned_to_me": assigned_to_me,
        "handoff_claimable": bool(active and assigned_user_id is None),
        "handoff_can_return_ai": bool(active and (assigned_to_me or manager_override)),
        "handoff_taken_over_at": handoff.taken_over_at if handoff else None,
    }


def _conversation_meta(item: AIConversation, agent: AIAgent, last_message, message_count: int, handoff=None):
    channel_type = item.channel_type or "unknown"
    handoff = handoff or {
        "mode": "ai",
        "handoff_reason": None,
        "handoff_status": None,
        "handoff_assigned_user_id": None,
        "handoff_assigned_to_me": False,
        "handoff_claimable": False,
        "handoff_can_return_ai": False,
        "handoff_taken_over_at": None,
    }
    capabilities = _handoff_capabilities(channel_type)
    return {
        "id": item.id,
        "title": item.title,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "channel_id": item.channel_id,
        "channel_type": channel_type,
        "channel_label": _channel_label(channel_type),
        "external_contact_id": item.external_contact_id,
        "created_at": item.created_at,
        "message_count": message_count,
        "mode": handoff["mode"],
        "handoff_reason": handoff["handoff_reason"],
        "handoff_status": handoff["handoff_status"],
        "handoff_assigned_user_id": handoff["handoff_assigned_user_id"],
        "handoff_assigned_to_me": handoff["handoff_assigned_to_me"],
        "handoff_claimable": handoff["handoff_claimable"],
        "handoff_can_return_ai": handoff["handoff_can_return_ai"],
        "handoff_taken_over_at": handoff["handoff_taken_over_at"],
        **capabilities,
        "last_message": (
            {
                "id": last_message.id,
                "role": last_message.role,
                "content": last_message.content[:300],
                "created_at": last_message.created_at,
            }
            if last_message
            else None
        ),
    }


def _authorized_conversation(db, current_user: User, conversation_id: int, *, lock: bool = False):
    agents = _visible_agents(db, current_user)
    agent_map = {item.id: item for item in agents}
    query = db.query(AIConversation).filter(
        AIConversation.id == conversation_id,
        AIConversation.company_id == current_user.company_id,
    )
    if lock:
        query = query.with_for_update()
    conversation = query.first()
    if conversation is None or conversation.agent_id not in agent_map:
        raise HTTPException(404, "Conversation not found")
    return conversation, agent_map[conversation.agent_id]


def _require_handoff_supported(conversation: AIConversation) -> dict:
    capabilities = _handoff_capabilities(conversation.channel_type)
    if not capabilities["handoff_supported"]:
        raise HTTPException(409, "Live human takeover is not available for this conversation channel yet")
    return capabilities


def _whatsapp_channel(db, conversation: AIConversation, session: WhatsAppSession):
    channels = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == conversation.company_id,
            AgentChannel.id == conversation.channel_id,
            AgentChannel.agent_id == conversation.agent_id,
            AgentChannel.channel_type == "whatsapp",
            AgentChannel.enabled.is_(True),
        )
        .all()
    )
    for channel in channels:
        config = reveal_config(channel.config) or {}
        if str(config.get("phone_number_id") or "") == str(session.phone_number_id or ""):
            return channel, config
    return None, None


def _audit_handoff(db, *, action: str, conversation: AIConversation, current_user: User, details: dict | None = None):
    payload = {
        "agent_id": conversation.agent_id,
        "channel_type": conversation.channel_type or "unknown",
        "external_contact_id": conversation.external_contact_id,
    }
    if details:
        payload.update(details)
    audit_service.log(
        db=db,
        action=action,
        resource_type="conversation",
        resource_id=conversation.id,
        user_id=current_user.id,
        company_id=current_user.company_id,
        details=payload,
    )


def _message_delivery_map(db, message_ids: list[int]) -> dict[int, dict]:
    if not message_ids:
        return {}
    rows = (
        db.query(WhatsAppOutboundDelivery)
        .filter(WhatsAppOutboundDelivery.message_id.in_(message_ids))
        .order_by(WhatsAppOutboundDelivery.id.desc())
        .all()
    )
    result = {}
    for row in rows:
        result.setdefault(
            row.message_id,
            {
                "id": row.id,
                "status": row.status,
                "retryable": bool(row.retryable),
                "provider_message_id": row.provider_message_id,
                "error_code": row.last_error_code,
            },
        )
    return result


@router.get("")
def list_inbox(
    agent_id: int | None = None,
    channel_type: str | None = None,
    channel_id: int | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    current_user: User = Depends(require_customer_operator),
):
    db = SessionLocal()
    try:
        agents = _visible_agents(db, current_user)
        agent_map = {item.id: item for item in agents}
        visible_ids = list(agent_map)
        if agent_id is not None and agent_id not in agent_map:
            raise HTTPException(403, "Conversation viewing is disabled for this AI employee")
        if not visible_ids:
            return {
                "conversations": [],
                "filters": {"agents": [], "channels": []},
                "paging": {"limit": 0, "offset": 0, "has_more": False},
            }

        safe_limit = max(1, min(int(limit or 100), 200))
        safe_offset = max(0, int(offset or 0))
        query = db.query(AIConversation).filter(
            AIConversation.company_id == current_user.company_id,
            AIConversation.agent_id.in_(visible_ids),
        )
        if agent_id is not None:
            query = query.filter(AIConversation.agent_id == agent_id)
        if channel_id is not None:
            query = query.filter(AIConversation.channel_id == channel_id)

        normalized_channel = str(channel_type or "").strip().lower()
        if normalized_channel:
            if normalized_channel == "unknown":
                query = query.filter(
                    or_(
                        AIConversation.channel_type.is_(None),
                        AIConversation.channel_type.in_(["", "unknown"]),
                    )
                )
            else:
                query = query.filter(AIConversation.channel_type == normalized_channel)
        else:
            query = query.filter(AIConversation.channel_type.in_(LIVE_INBOX_CHANNELS))

        search_value = str(search or "").strip()
        if search_value:
            pattern = f"%{search_value}%"
            query = query.filter(
                or_(
                    AIConversation.title.ilike(pattern),
                    AIConversation.external_contact_id.ilike(pattern),
                )
            )

        latest_message_id = (
            db.query(func.max(AIMessage.id))
            .filter(AIMessage.conversation_id == AIConversation.id)
            .correlate(AIConversation)
            .scalar_subquery()
        )
        conversations = (
            query.order_by(latest_message_id.desc().nullslast(), AIConversation.id.desc())
            .offset(safe_offset)
            .limit(safe_limit + 1)
            .all()
        )
        has_more = len(conversations) > safe_limit
        conversations = conversations[:safe_limit]
        conversation_ids = [item.id for item in conversations]

        last_messages: dict[int, AIMessage] = {}
        counts = {item.id: 0 for item in conversations}
        if conversation_ids:
            aggregates = (
                db.query(
                    AIMessage.conversation_id,
                    func.count(AIMessage.id),
                    func.max(AIMessage.id),
                )
                .filter(AIMessage.conversation_id.in_(conversation_ids))
                .group_by(AIMessage.conversation_id)
                .all()
            )
            last_ids = []
            for conversation_id, message_count, last_id in aggregates:
                counts[int(conversation_id)] = int(message_count or 0)
                if last_id is not None:
                    last_ids.append(int(last_id))
            if last_ids:
                for message in db.query(AIMessage).filter(AIMessage.id.in_(last_ids)).all():
                    last_messages[message.conversation_id] = message

        assigned_channels = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id.in_(visible_ids),
                AgentChannel.enabled.is_(True),
            )
            .all()
        )
        channel_types = {
            str(item.channel_type).strip().lower()
            for item in assigned_channels
            if item.channel_type
        }
        channel_types.update((item.channel_type or "unknown").strip().lower() for item in conversations)

        return {
            "conversations": [
                _conversation_meta(
                    item,
                    agent_map[item.agent_id],
                    last_messages.get(item.id),
                    counts.get(item.id, 0),
                    _handoff_state(db, current_user, item.id),
                )
                for item in conversations
            ],
            "filters": {
                "agents": [{"id": item.id, "name": item.name} for item in agents],
                "channels": [
                    {"type": value, "label": _channel_label(value)}
                    for value in sorted(channel_types)
                ],
            },
            "paging": {
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": has_more,
                "next_offset": safe_offset + safe_limit if has_more else None,
            },
        }
    finally:
        db.close()


@router.get("/{conversation_id}")
def inbox_conversation(
    conversation_id: int,
    current_user: User = Depends(require_customer_operator),
):
    db = SessionLocal()
    try:
        conversation, agent = _authorized_conversation(db, current_user, conversation_id)
        messages = (
            db.query(AIMessage)
            .filter(AIMessage.conversation_id == conversation.id)
            .order_by(AIMessage.id.asc())
            .all()
        )
        delivery_map = _message_delivery_map(db, [item.id for item in messages])
        return {
            "conversation": _conversation_meta(
                conversation,
                agent,
                messages[-1] if messages else None,
                len(messages),
                _handoff_state(db, current_user, conversation.id),
            ),
            "messages": [
                {
                    "id": item.id,
                    "role": item.role,
                    "content": item.content,
                    "created_at": item.created_at,
                    "delivery": delivery_map.get(item.id),
                }
                for item in messages
            ],
        }
    finally:
        db.close()


@router.post("/{conversation_id}/take-over")
def take_over_conversation(
    conversation_id: int,
    current_user: User = Depends(require_customer_operator),
):
    db = SessionLocal()
    try:
        conversation, _ = _authorized_conversation(db, current_user, conversation_id, lock=True)
        capabilities = _require_handoff_supported(conversation)
        handoff = _active_handoff(db, current_user.company_id, conversation.id)
        if handoff is None:
            handoff = HumanHandoff(
                company_id=current_user.company_id,
                agent_id=conversation.agent_id,
                conversation_id=conversation.id,
                reason="customer_portal_takeover",
                priority="normal",
                department="customer_service",
                status="pending",
            )
            db.add(handoff)
            db.flush()

        _claim_handoff(handoff, current_user)
        session = _session(db, current_user.company_id, conversation.id)
        if session is not None:
            activate_human_handoff(session, reason=handoff.reason or "customer_portal_takeover")
        _audit_handoff(
            db,
            action="customer_inbox.handoff_started",
            conversation=conversation,
            current_user=current_user,
            details={
                "handoff_reason": handoff.reason,
                "delivery": capabilities["human_reply_delivery"],
                "assigned_user_id": current_user.id,
            },
        )
        db.commit()
        return {
            "status": "human_active",
            "conversation_id": conversation.id,
            "mode": "human",
            "handoff_assigned_user_id": current_user.id,
            "handoff_assigned_to_me": True,
            "handoff_claimable": False,
            "handoff_can_return_ai": True,
            **capabilities,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{conversation_id}/return-ai")
def return_conversation_to_ai(
    conversation_id: int,
    current_user: User = Depends(require_customer_operator),
):
    db = SessionLocal()
    try:
        conversation, _ = _authorized_conversation(db, current_user, conversation_id, lock=True)
        handoffs = (
            db.query(HumanHandoff)
            .filter(
                HumanHandoff.company_id == current_user.company_id,
                HumanHandoff.conversation_id == conversation.id,
                HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
            )
            .all()
        )
        for handoff in handoffs:
            _require_handoff_close_permission(handoff, current_user)

        session = _session(db, current_user.company_id, conversation.id)
        if (
            not handoffs
            and session is not None
            and human_handoff_active(session)
            and current_user.role not in HANDOFF_MANAGER_ROLES
        ):
            raise HTTPException(409, "A manager must return this unassigned conversation to AI")
        marker = (
            whatsapp_job_queue.human_marker(session.phone_number_id, session.wa_id)
            if session
            else None
        )
        completed_at = datetime.utcnow()
        for handoff in handoffs:
            handoff.status = "completed"
            handoff.completed_at = completed_at
            handoff.updated_at = completed_at

        session = _session(db, current_user.company_id, conversation.id)
        if session is not None:
            resume_ai(session)
        _audit_handoff(
            db,
            action="customer_inbox.ai_resumed",
            conversation=conversation,
            current_user=current_user,
            details={
                "completed_handoffs": len(handoffs),
                "override": any(
                    item.assigned_user_id not in {None, current_user.id}
                    for item in handoffs
                ),
            },
        )
        db.commit()
        if session:
            whatsapp_job_queue.clear_human_marker(
                session.phone_number_id,
                session.wa_id,
                marker,
            )
        return {"status": "ai_active", "conversation_id": conversation.id, "mode": "ai"}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{conversation_id}/message")
def send_human_reply(
    conversation_id: int,
    data: HumanReply,
    current_user: User = Depends(require_customer_operator),
):
    db = SessionLocal()
    try:
        conversation, _ = _authorized_conversation(db, current_user, conversation_id, lock=True)
        capabilities = _require_handoff_supported(conversation)
        if not capabilities["human_reply_supported"]:
            raise HTTPException(409, "Human replies are not available for this conversation channel yet")

        handoff = _active_handoff(db, current_user.company_id, conversation.id)
        if handoff is None:
            raise HTTPException(409, "Take over the conversation before replying")
        _require_handoff_owner(handoff, current_user)

        text = data.message.strip()
        delivery = capabilities["human_reply_delivery"]
        audit_details = {"delivery": delivery, "assigned_user_id": current_user.id}
        message = None
        delivery_row = None
        delivery_state = None

        if delivery == "whatsapp":
            session = _session(db, current_user.company_id, conversation.id)
            if session is None:
                raise HTTPException(409, "WhatsApp session is unavailable for this conversation")
            _channel, config = _whatsapp_channel(db, conversation, session)
            if config is None:
                raise HTTPException(409, "The WhatsApp channel for this conversation is not active or configured")

            client_message_id = data.client_message_id or str(uuid4())
            idempotency_key = f"portal-human:{current_user.company_id}:{conversation.id}:{current_user.id}:{client_message_id}"
            existing_delivery = (
                db.query(WhatsAppOutboundDelivery)
                .filter(WhatsAppOutboundDelivery.idempotency_key == idempotency_key)
                .first()
            )
            if existing_delivery is not None:
                existing_message = db.get(AIMessage, existing_delivery.message_id)
                if existing_message is None or existing_message.content != text:
                    raise HTTPException(409, "This client message identity is already used by a different reply")
                result = delivery_payload(existing_delivery)
                if existing_delivery.status in {"accepted", "delivered", "read"}:
                    return {
                        "status": "sent",
                        "mode": "human",
                        "delivery": delivery,
                        "delivery_state": result,
                        "message": {
                            "id": existing_message.id,
                            "role": existing_message.role,
                            "content": existing_message.content,
                            "created_at": existing_message.created_at,
                        },
                    }
                if existing_delivery.status == "unknown":
                    raise HTTPException(409, "Previous WhatsApp delivery outcome is unknown; do not resend blindly")
                if existing_delivery.status == "failed" and not existing_delivery.retryable:
                    raise HTTPException(502, "Previous WhatsApp delivery was rejected and cannot be retried safely")
                delivery_row = existing_delivery
                message = existing_message
            else:
                message = AIMessage(conversation_id=conversation.id, role="human", content=text)
                db.add(message)
                db.flush()
                delivery_row = ensure_delivery(
                    db,
                    idempotency_key=idempotency_key,
                    company_id=current_user.company_id,
                    agent_id=conversation.agent_id,
                    conversation_id=conversation.id,
                    channel_id=conversation.channel_id,
                    message_id=message.id,
                    wa_id=session.wa_id,
                )
                activate_human_handoff(
                    session,
                    reason=handoff.reason or "customer_portal_reply",
                    human_message=True,
                )
                handoff.status = "in_progress"
                handoff.updated_at = datetime.utcnow()
                _audit_handoff(
                    db,
                    action="customer_inbox.human_reply_prepared",
                    conversation=conversation,
                    current_user=current_user,
                    details={
                        **audit_details,
                        "delivery_id": delivery_row.id,
                    },
                )
                db.commit()

            result = attempt_delivery(db, delivery_id=delivery_row.id, config=config)
            if not result.get("success"):
                if result.get("unknown"):
                    raise HTTPException(502, "WhatsApp delivery outcome is unknown; the reply is recorded for reconciliation and will not be resent automatically")
                if result.get("retryable"):
                    raise HTTPException(503, "WhatsApp delivery was rejected temporarily; the saved reply can be retried without duplication")
                raise HTTPException(502, "WhatsApp delivery failed; the saved reply needs review")
            audit_details.update({"delivery_id": delivery_row.id, "delivery_status": result.get("status")})
        elif delivery == "website_widget":
            message = AIMessage(conversation_id=conversation.id, role="human", content=text)
            db.add(message)
            handoff.status = "in_progress"
            handoff.updated_at = datetime.utcnow()
        elif delivery == "n8n_channel":
            channel = (
                db.query(AgentChannel)
                .filter(
                    AgentChannel.id == conversation.channel_id,
                    AgentChannel.company_id == current_user.company_id,
                    AgentChannel.agent_id == conversation.agent_id,
                    AgentChannel.enabled.is_(True),
                )
                .first()
            )
            capability = (
                get_channel_capability(channel.channel_type)
                if channel is not None
                else None
            ) or {}
            if (
                channel is None
                or capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
                or capability.get("runtime_adapter") != N8N_CHANNEL_RUNTIME_ADAPTER
            ):
                raise HTTPException(409, "The managed channel for this conversation is not active")
            external_contact_id = str(conversation.external_contact_id or "").strip()
            if not external_contact_id:
                raise HTTPException(409, "The managed channel contact identity is unavailable")

            client_message_id = data.client_message_id or str(uuid4())
            source_key = (
                f"portal-human-n8n:{current_user.company_id}:"
                f"{conversation.id}:{current_user.id}:{client_message_id}"
            )
            if len(source_key) > 320:
                raise HTTPException(400, "Client message identity is too long")
            message = (
                db.query(AIMessage)
                .filter(AIMessage.source_key == source_key)
                .first()
            )
            if message is not None and message.content != text:
                raise HTTPException(
                    409,
                    "This client message identity is already used by a different reply",
                )
            if message is None:
                message = AIMessage(
                    conversation_id=conversation.id,
                    role="human",
                    content=text,
                    source_key=source_key,
                )
                db.add(message)
                db.flush()
                handoff.status = "in_progress"
                handoff.updated_at = datetime.utcnow()
                _audit_handoff(
                    db,
                    action="customer_inbox.human_reply_prepared",
                    conversation=conversation,
                    current_user=current_user,
                    details={
                        **audit_details,
                        "channel_id": channel.id,
                        "message_id": message.id,
                    },
                )
                db.commit()
                db.refresh(message)

            idempotency_key = source_key
            try:
                delivery_state = n8n_channel_gateway.send_message(
                    company_id=current_user.company_id,
                    agent_id=conversation.agent_id,
                    channel_id=channel.id,
                    channel_type=canonical_channel_type(channel.channel_type),
                    external_contact_id=external_contact_id,
                    message=text,
                    idempotency_key=idempotency_key,
                )
            except N8NChannelGatewayError as exc:
                raise HTTPException(
                    503,
                    "Managed channel delivery is temporarily unavailable",
                ) from exc
            if not delivery_state.get("success"):
                raise HTTPException(502, "Managed channel delivery failed")
            audit_details.update(
                {
                    "channel_id": channel.id,
                    "provider_message_id": (
                        delivery_state.get("data") or {}
                    ).get("provider_message_id"),
                }
            )
        else:
            raise HTTPException(409, "No human reply delivery adapter exists for this channel")

        if delivery == "whatsapp":
            db.refresh(message)
        _audit_handoff(
            db,
            action="customer_inbox.human_reply_sent",
            conversation=conversation,
            current_user=current_user,
            details=audit_details,
        )
        db.commit()
        if delivery != "whatsapp":
            db.refresh(message)
        return {
            "status": "sent",
            "mode": "human",
            "delivery": delivery,
            "delivery_state": (
                delivery_payload(delivery_row)
                if delivery_row is not None
                else delivery_state
            ),
            "message": {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
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
