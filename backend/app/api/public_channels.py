import hmac
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from backend.app.api.website_widget import (
    _assistance_mode,
    website_behavior,
    website_contact_methods,
)
from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.visitor_tokens import (
    VisitorTokenError,
    issue_website_visitor_token,
    verify_website_visitor_token,
)
from backend.app.modules.ai_agent.models import AIConversation, AIMessage
from backend.app.modules.ai_agent.profile_models import AIAgentProfile
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.acceptance import mark_customer_roundtrip
from backend.app.modules.channels.conversation_source import bind_conversation_source
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.vapi import build_voice_behavior_prompt
from backend.app.modules.tools.business_models import HumanHandoff

router = APIRouter(prefix="/channels", tags=["Public Agent Channels"])


class WebsiteChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: int | None = None


class VoiceTurnInput(BaseModel):
    transcript: str = Field(min_length=1, max_length=12000)
    conversation_id: int | None = None
    session_id: str | None = None


ACTIVE_HANDOFF_STATUSES = {"pending", "in_progress"}


def secure_equal(received, expected):
    return bool(
        received
        and expected
        and hmac.compare_digest(str(received), str(expected))
    )


def host(value):
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").lower().strip(".")


def website_origin_allowed(origin, allowed_domain):
    origin_host = host(origin)
    allowed_host = host(allowed_domain)
    return bool(
        origin_host
        and allowed_host
        and (
            origin_host == allowed_host
            or origin_host.endswith("." + allowed_host)
        )
    )


def require_channel(db, channel_id, channel_type):
    channel = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.id == channel_id,
            AgentChannel.channel_type == channel_type,
            AgentChannel.enabled.is_(True),
        )
        .first()
    )
    if channel is None:
        raise HTTPException(404, "Channel unavailable")
    return channel


def _website_auth(db, channel_id, request, key):
    channel = require_channel(db, channel_id, "website")
    config = reveal_config(channel.config) or {}
    if not secure_equal(key, config.get("widget_key")):
        raise HTTPException(401, "Invalid widget key")
    if not website_origin_allowed(
        request.headers.get("origin"), config.get("allowed_domain")
    ):
        raise HTTPException(403, "Website origin is not allowed")
    return channel, config


def _require_visitor_token(token, channel_id: int, conversation_id: int):
    try:
        return verify_website_visitor_token(
            token,
            channel_id=channel_id,
            conversation_id=conversation_id,
        )
    except VisitorTokenError as exc:
        raise HTTPException(401, str(exc)) from exc


def _human_active(db, company_id, conversation_id):
    if not conversation_id:
        return False
    return (
        db.query(HumanHandoff)
        .filter(
            HumanHandoff.company_id == company_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
        )
        .first()
        is not None
    )


def _conversation(db, channel: AgentChannel, conversation_id: int):
    conversation = (
        db.query(AIConversation)
        .filter(
            AIConversation.id == conversation_id,
            AIConversation.company_id == channel.company_id,
            AIConversation.agent_id == channel.agent_id,
            AIConversation.channel_id == channel.id,
        )
        .first()
    )
    if conversation is None:
        raise HTTPException(404, "Conversation not found")
    return conversation


def _dominant_message_language(message: str) -> str:
    text = str(message or "")
    arabic = sum(1 for char in text if "\u0600" <= char <= "\u06ff")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    if latin > arabic:
        return "en"
    if arabic > latin:
        return "ar"
    return "auto"


def _website_language_policy(db, channel: AgentChannel, message: str) -> str:
    profile = (
        db.query(AIAgentProfile)
        .filter(
            AIAgentProfile.company_id == channel.company_id,
            AIAgentProfile.agent_id == channel.agent_id,
        )
        .first()
    )
    configured = str(profile.reply_language if profile else "auto").strip().lower()
    aliases = {
        "english": "en",
        "arabic": "ar",
        "en-us": "en",
        "en-gb": "en",
    }
    configured = aliases.get(configured, configured)
    selected = _dominant_message_language(message) if configured == "auto" else configured
    if selected == "en":
        return (
            "CURRENT RESPONSE LANGUAGE (highest priority for this turn): English. "
            "Reply entirely in natural English because the customer's current message is English. "
            "Do not answer in Arabic merely because company knowledge or earlier messages are Arabic."
        )
    if selected == "ar":
        return (
            "CURRENT RESPONSE LANGUAGE (highest priority for this turn): Arabic. "
            "Reply in Arabic and follow the configured Arabic dialect policy."
        )
    return (
        "CURRENT RESPONSE LANGUAGE: follow the customer's current message language. "
        "If the customer changes language, change the reply language on the same turn."
    )


def _is_service_access_error(exc: HTTPException) -> bool:
    detail = exc.detail
    if isinstance(detail, dict):
        message = str(detail.get("message") or "").lower()
        return bool(detail.get("service")) and (
            "limit" in message or "capacity" in message
        )
    text = str(detail or "").lower()
    return "service subscription" in text or "service plan" in text


def _safe_unavailable_message(message: str, config: dict) -> str:
    language = _dominant_message_language(message)
    mode = _assistance_mode(config)
    contacts = website_contact_methods(config)
    if language == "en":
        if mode == "direct_handoff":
            return "Sorry, the service is temporarily unavailable. I've forwarded your conversation to the team for assistance."
        if mode == "contact_only":
            return "Sorry, the service is temporarily unavailable. You can contact the team here:\n" + "\n".join(contacts)
        return "Sorry, the service is temporarily unavailable. Please try again later."
    if mode == "direct_handoff":
        return "عذرًا، الخدمة غير متاحة مؤقتًا. تم تحويل محادثتك للفريق لمساعدتك."
    if mode == "contact_only":
        return "عذرًا، الخدمة غير متاحة مؤقتًا. يمكنك التواصل مع الفريق عبر:\n" + "\n".join(contacts)
    return "عذرًا، الخدمة غير متاحة مؤقتًا. يرجى المحاولة لاحقًا."


def _website_service_fallback(
    db,
    *,
    channel: AgentChannel,
    config: dict,
    message: str,
    conversation_id: int | None,
    internal_error: HTTPException,
):
    db.rollback()
    mode = _assistance_mode(config)
    conversation = agent_runtime.get_or_create_conversation(
        db=db,
        company_id=channel.company_id,
        agent_id=channel.agent_id,
        conversation_id=conversation_id,
        message=message,
    )
    bind_conversation_source(
        db,
        conversation_id=conversation.id,
        company_id=channel.company_id,
        agent_id=channel.agent_id,
        channel_type="website",
        channel_id=channel.id,
    )
    user_message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content=message.strip(),
    )
    safe_text = _safe_unavailable_message(message, config)
    assistant_message = AIMessage(
        conversation_id=conversation.id,
        role="assistant",
        content=safe_text,
    )
    db.add(user_message)
    db.add(assistant_message)
    if mode == "direct_handoff" and not _human_active(
        db, channel.company_id, conversation.id
    ):
        db.add(
            HumanHandoff(
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                conversation_id=conversation.id,
                reason="service_limit_or_entitlement",
                priority="high",
                department="customer_service",
                status="pending",
            )
        )
    audit_service.log(
        db=db,
        company_id=channel.company_id,
        action="website.customer_service_fallback",
        resource_type="channel",
        resource_id=channel.id,
        details={
            "conversation_id": conversation.id,
            "human_assistance_mode": mode,
            "internal_status": internal_error.status_code,
            "internal_detail": internal_error.detail,
        },
    )
    db.commit()
    db.refresh(user_message)
    db.refresh(assistant_message)
    return {
        "conversation_id": conversation.id,
        "visitor_token": issue_website_visitor_token(channel.id, conversation.id),
        "mode": "human" if mode == "direct_handoff" else "ai",
        "message": {
            "id": user_message.id,
            "role": "user",
            "content": user_message.content,
        },
        "response": {
            "id": assistant_message.id,
            "role": "assistant",
            "content": assistant_message.content,
        },
    }


@router.post("/website/{channel_id}/chat")
def website_chat(
    channel_id: int,
    data: WebsiteChatInput,
    request: Request,
    x_xvond_widget_key: str | None = Header(default=None),
    x_xvond_visitor_token: str | None = Header(default=None),
):
    db = SessionLocal()
    try:
        channel, config = _website_auth(
            db, channel_id, request, x_xvond_widget_key
        )

        if data.conversation_id is not None:
            _require_visitor_token(
                x_xvond_visitor_token,
                channel.id,
                data.conversation_id,
            )

        if data.conversation_id and _human_active(
            db, channel.company_id, data.conversation_id
        ):
            conversation = _conversation(db, channel, data.conversation_id)
            bind_conversation_source(
                db,
                conversation_id=conversation.id,
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                channel_type="website",
                channel_id=channel.id,
            )
            msg = AIMessage(
                conversation_id=conversation.id,
                role="user",
                content=data.message.strip(),
            )
            db.add(msg)
            db.commit()
            db.refresh(msg)
            return {
                "conversation_id": conversation.id,
                "visitor_token": issue_website_visitor_token(
                    channel.id, conversation.id
                ),
                "mode": "human",
                "message": {
                    "id": msg.id,
                    "role": "user",
                    "content": msg.content,
                },
                "response": None,
            }

        agent = agent_runtime.get_agent(
            db, channel.company_id, channel.agent_id
        )
        original_prompt = agent.system_prompt
        try:
            language_policy = _website_language_policy(db, channel, data.message)
            runtime_prompt = (
                original_prompt
                + "\n\n"
                + website_behavior(config)
                + "\n\n"
                + language_policy
            ).strip()
            try:
                result = agent_runtime.chat(
                    db=db,
                    company_id=channel.company_id,
                    agent_id=channel.agent_id,
                    message=data.message,
                    conversation_id=data.conversation_id,
                    channel_type="website", channel_id=channel.id,
                    system_prompt_override=runtime_prompt, commit=False,
                )
            except HTTPException as exc:
                if _is_service_access_error(exc):
                    return _website_service_fallback(
                        db,
                        channel=channel,
                        config=config,
                        message=data.message,
                        conversation_id=data.conversation_id,
                        internal_error=exc,
                    )
                raise
            bind_conversation_source(
                db,
                conversation_id=result["conversation_id"],
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                channel_type="website",
                channel_id=channel.id,
            )
            mark_customer_roundtrip(
                channel,
                source="website_ai_response",
            )
            db.commit()
            result["visitor_token"] = issue_website_visitor_token(
                channel.id, result["conversation_id"]
            )
            result["mode"] = (
                "human"
                if _human_active(
                    db, channel.company_id, result["conversation_id"]
                )
                else "ai"
            )
            return result
        finally:
            agent.system_prompt = original_prompt
    finally:
        db.close()


@router.get("/website/{channel_id}/conversation/{conversation_id}/messages")
def website_messages(
    channel_id: int,
    conversation_id: int,
    request: Request,
    after_id: int = 0,
    x_xvond_widget_key: str | None = Header(default=None),
    x_xvond_visitor_token: str | None = Header(default=None),
):
    db = SessionLocal()
    try:
        channel, _config = _website_auth(
            db, channel_id, request, x_xvond_widget_key
        )
        _require_visitor_token(
            x_xvond_visitor_token,
            channel.id,
            conversation_id,
        )
        conversation = _conversation(db, channel, conversation_id)
        messages_query = db.query(AIMessage).filter(
            AIMessage.conversation_id == conversation.id,
        )
        if after_id > 0:
            items = (
                messages_query
                .filter(AIMessage.id > after_id)
                .order_by(AIMessage.id.asc())
                .limit(100)
                .all()
            )
        else:
            # Initial widget restore should show the most recent conversation
            # context immediately instead of walking old history page by page.
            items = list(
                reversed(
                    messages_query
                    .order_by(AIMessage.id.desc())
                    .limit(100)
                    .all()
                )
            )
        return {
            "mode": (
                "human"
                if _human_active(db, channel.company_id, conversation.id)
                else "ai"
            ),
            "messages": [
                {"id": x.id, "role": x.role, "content": x.content}
                for x in items
            ],
        }
    finally:
        db.close()


@router.post("/voice/{channel_id}/turn")
def voice_turn(
    channel_id: int,
    data: VoiceTurnInput,
    x_xvond_voice_token: str | None = Header(default=None),
):
    db = SessionLocal()
    try:
        channel = require_channel(db, channel_id, "voice")
        config = reveal_config(channel.config) or {}
        provider = str(config.get("provider") or "").strip().lower()
        if provider == "vapi":
            raise HTTPException(
                404,
                "Vapi voice channels use the dedicated voice LLM callback endpoint",
            )
        if not secure_equal(x_xvond_voice_token, config.get("auth_token")):
            raise HTTPException(401, "Invalid voice channel token")

        agent = agent_runtime.get_agent(db, channel.company_id, channel.agent_id)
        original_prompt = agent.system_prompt or ""
        try:
            runtime_prompt = (
                original_prompt + "\n\n" + build_voice_behavior_prompt(config)
            ).strip()
            result = agent_runtime.chat(
                db=db,
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                message=data.transcript,
                conversation_id=data.conversation_id,
                channel_type="voice", channel_id=channel.id,
                external_contact_id=data.session_id,
                system_prompt_override=runtime_prompt, commit=False,
            )
        finally:
            agent.system_prompt = original_prompt

        bind_conversation_source(
            db,
            conversation_id=result["conversation_id"],
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            channel_type="voice",
            channel_id=channel.id,
            external_contact_id=data.session_id,
        )
        mark_customer_roundtrip(
            channel,
            source="voice_turn_response",
        )
        db.commit()
        return {
            "channel_id": channel.id,
            "session_id": data.session_id,
            "conversation_id": result["conversation_id"],
            "text": result["response"]["content"],
        }
    finally:
        db.close()
