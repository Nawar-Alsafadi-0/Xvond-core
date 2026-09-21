from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.api.admin_ai_employee_profile import (
    EmployeeProfileUpdate,
    _agent_behavior,
    _backfill_profile,
    _profile_prompt,
    _set_agent_behavior,
    _upsert_profile,
)
from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.channels.catalog import get_channel_capability
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.vapi import build_vapi_assistant_payload
from backend.app.modules.channels.vapi_api import update_assistant


router = APIRouter(prefix="/customer/agents", tags=["Customer Agent Controls"])


class CustomerChannelBehaviorUpdate(BaseModel):
    tone: str | None = None
    response_style: str | None = None
    response_length: str | None = None
    channel_instructions: str | None = None


class CustomerVoiceSettingsUpdate(BaseModel):
    tone: str | None = None
    response_length: str | None = None
    greeting_message: str | None = None
    channel_instructions: str | None = None
    allow_interruption: bool | None = None


class AgentCustomerUpdate(BaseModel):
    enabled: bool | None = None
    reply_language: str | None = None
    dialect: str | None = None
    conversation_style: str | None = None
    response_length: str | None = None
    clarification_style: str | None = None
    off_topic_behavior: str | None = None
    greeting: str | None = None
    instructions: str | None = None


def get_customer_agent(db, user: User, agent_id: int):
    agent = (
        db.query(AIAgent)
        .filter(AIAgent.id == agent_id, AIAgent.company_id == user.company_id)
        .first()
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="AI Agent not found")
    return agent


def _controls(config: AgentConfig | None) -> dict:
    return dict(config.customer_controls or {}) if config else {}


def _current_profile(db, company: Company, agent: AIAgent):
    channels = (
        db.query(AgentChannel)
        .filter(AgentChannel.company_id == company.id, AgentChannel.agent_id == agent.id)
        .order_by(AgentChannel.id.asc())
        .all()
    )
    # Channel employee_setup is read only as a legacy migration fallback. New
    # employee state is owned by AIAgentProfile + AgentConfig and is never
    # duplicated back into channel configuration.
    profile = _backfill_profile(db, company, agent, channels)
    behavior = _agent_behavior(db, agent, channels)
    return profile, behavior




_GENERIC_CUSTOMER_BEHAVIOR_CHANNELS = {"telegram", "email", "sms", "slack", "teams", "custom"}


def _customer_voice_channel(db, user: User, agent_id: int) -> tuple[AIAgent, AgentChannel]:
    agent = get_customer_agent(db, user, agent_id)
    channel = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == user.company_id,
            AgentChannel.agent_id == agent.id,
            AgentChannel.channel_type == "voice",
        )
        .first()
    )
    if channel is None:
        raise HTTPException(404, "Voice channel is not assigned to this AI Employee")
    return agent, channel


@router.get("/voice/{agent_id}/settings")
def customer_voice_settings(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent, channel = _customer_voice_channel(db, current_user, agent_id)
        config = reveal_config(channel.config) or {}
        return {
            "agent_id": agent.id,
            "channel_id": channel.id,
            "channel_type": "voice",
            "phone_number": config.get("phone_number"),
            "connected": bool(
                str(config.get("provider") or "").lower() == "vapi"
                and config.get("vapi_assistant_id")
                and config.get("vapi_phone_number_id")
            ),
            "settings": {
                "tone": str(config.get("tone") or "professional_friendly"),
                "response_length": str(config.get("response_length") or "concise"),
                "greeting_message": str(config.get("greeting_message") or ""),
                "channel_instructions": str(config.get("channel_instructions") or ""),
                "allow_interruption": bool(config.get("allow_interruption", True)),
            },
        }
    finally:
        db.close()


@router.put("/voice/{agent_id}/settings")
def update_customer_voice_settings(
    agent_id: int,
    data: CustomerVoiceSettingsUpdate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent, channel = _customer_voice_channel(db, current_user, agent_id)
        channel = (
            db.query(AgentChannel)
            .filter(AgentChannel.id == channel.id)
            .with_for_update()
            .first()
        )
        current = reveal_config(channel.config) or {}
        updates = {
            "tone": str(data.tone or current.get("tone") or "professional_friendly").strip()[:80],
            "response_length": str(data.response_length or current.get("response_length") or "concise").strip()[:80],
            "greeting_message": str(data.greeting_message or "").strip()[:1000] or None,
            "channel_instructions": str(data.channel_instructions or "").strip()[:4000] or None,
            "allow_interruption": (
                bool(data.allow_interruption)
                if data.allow_interruption is not None
                else bool(current.get("allow_interruption", True))
            ),
        }
        merged = {**current, **updates}

        assistant_id = str(current.get("vapi_assistant_id") or "").strip()
        credential_id = str(current.get("vapi_llm_credential_id") or "").strip()
        model_url = str(current.get("vapi_model_url") or "").strip()
        if assistant_id and credential_id and model_url:
            payload = build_vapi_assistant_payload(
                assistant_name=(str(agent.name or "Xvond Voice Agent").strip()[:40] or "Xvond Voice Agent"),
                model_url=model_url,
                channel_config=merged,
                credential_id=credential_id,
            )
            update_assistant(assistant_id, payload)

        channel.config = merge_config(channel.config, updates)
        db.commit()
        return {
            "status": "updated",
            "agent_id": agent.id,
            "channel_id": channel.id,
            "channel_type": "voice",
            "provider_synced": bool(assistant_id and credential_id and model_url),
            "settings": updates,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()




def _customer_behavior_channel(db, user: User, agent_id: int, channel_type: str) -> AgentChannel:
    key = str(channel_type or "").strip().lower()
    if key not in _GENERIC_CUSTOMER_BEHAVIOR_CHANNELS:
        raise HTTPException(400, "This channel uses its dedicated settings flow")
    agent = get_customer_agent(db, user, agent_id)
    channel = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == user.company_id,
            AgentChannel.agent_id == agent.id,
            AgentChannel.channel_type == key,
        )
        .first()
    )
    if channel is None:
        raise HTTPException(404, "Channel is not assigned to this AI Employee")
    capability = get_channel_capability(key) or {}
    if capability.get("runtime_state") != "live":
        raise HTTPException(409, "Channel runtime is not available")
    return channel


@router.get("/{agent_id}/channels/{channel_type}/settings")
def customer_channel_behavior_settings(
    agent_id: int,
    channel_type: str,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        channel = _customer_behavior_channel(db, current_user, agent_id, channel_type)
        config = reveal_config(channel.config) or {}
        return {
            "agent_id": agent_id,
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "settings": {
                "tone": str(config.get("tone") or "professional_friendly"),
                "response_style": str(config.get("response_style") or "conversational"),
                "response_length": str(config.get("response_length") or "concise"),
                "channel_instructions": str(config.get("channel_instructions") or ""),
            },
        }
    finally:
        db.close()


@router.put("/{agent_id}/channels/{channel_type}/settings")
def update_customer_channel_behavior_settings(
    agent_id: int,
    channel_type: str,
    data: CustomerChannelBehaviorUpdate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        channel = _customer_behavior_channel(db, current_user, agent_id, channel_type)
        channel = (
            db.query(AgentChannel)
            .filter(AgentChannel.id == channel.id)
            .with_for_update()
            .first()
        )
        current = reveal_config(channel.config) or {}
        updates = {
            "tone": str(data.tone or current.get("tone") or "professional_friendly").strip()[:80],
            "response_style": str(data.response_style or current.get("response_style") or "conversational").strip()[:80],
            "response_length": str(data.response_length or current.get("response_length") or "concise").strip()[:80],
            "channel_instructions": str(data.channel_instructions or "").strip()[:4000] or None,
        }
        channel.config = merge_config(channel.config, updates)
        db.commit()
        return {
            "status": "updated",
            "agent_id": agent_id,
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "settings": updates,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

@router.get("/{agent_id}")
def agent_details(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = get_customer_agent(db, current_user, agent_id)
        company = db.query(Company).filter(Company.id == current_user.company_id).first()
        profile, behavior = _current_profile(db, company, agent)
        config = db.query(AgentConfig).filter(AgentConfig.agent_id == agent.id).first()
        controls = _controls(config)
        db.commit()
        return {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "enabled": agent.enabled,
            "reply_language": profile.reply_language or "auto",
            "dialect": behavior["dialect"],
            "conversation_style": profile.conversation_style or "professional_friendly",
            "response_length": behavior["response_length"],
            "clarification_style": behavior["clarification_style"],
            "off_topic_behavior": behavior["off_topic_behavior"],
            "greeting": profile.greeting or "",
            "instructions": (profile.instructions or "") if controls.get("can_edit_prompt") else "",
            "controls": controls,
        }
    finally:
        db.close()


@router.patch("/{agent_id}")
def update_agent(
    agent_id: int,
    data: AgentCustomerUpdate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = get_customer_agent(db, current_user, agent_id)
        company = db.query(Company).filter(Company.id == current_user.company_id).first()
        profile, behavior = _current_profile(db, company, agent)
        config = db.query(AgentConfig).filter(AgentConfig.agent_id == agent.id).first()
        controls = _controls(config)

        if data.enabled is not None:
            if not controls.get("can_enable_disable", False):
                raise HTTPException(403, "Customer cannot change this employee's runtime state")
            if data.enabled is True and agent.enabled is False:
                # Customer managers may stop a live employee, but production
                # activation is owned exclusively by Xvond Delivery Readiness.
                raise HTTPException(
                    409,
                    "AI employee activation is managed by Xvond Delivery Readiness",
                )
            if data.enabled is False:
                agent.enabled = False

        if data.instructions is not None and not controls.get("can_edit_prompt", False):
            raise HTTPException(403, "Advanced instructions are managed by Xvond")

        behavior_requested = any(
            value is not None
            for value in (
                data.reply_language,
                data.dialect,
                data.conversation_style,
                data.response_length,
                data.clarification_style,
                data.off_topic_behavior,
                data.greeting,
                data.instructions,
            )
        )
        if behavior_requested:
            update = EmployeeProfileUpdate(
                name=agent.name,
                reply_language=data.reply_language or profile.reply_language or "auto",
                dialect=data.dialect or behavior["dialect"],
                conversation_style=data.conversation_style or profile.conversation_style or "professional_friendly",
                response_length=data.response_length or behavior["response_length"],
                clarification_style=data.clarification_style or behavior["clarification_style"],
                off_topic_behavior=data.off_topic_behavior or behavior["off_topic_behavior"],
                greeting=profile.greeting if data.greeting is None else data.greeting,
                instructions=profile.instructions if data.instructions is None else data.instructions,
            )
            agent.system_prompt = _profile_prompt(company.name, update)
            _upsert_profile(db, company, agent, update)
            _set_agent_behavior(db, agent, update)

        db.commit()
        db.refresh(agent)
        return {"id": agent.id, "enabled": agent.enabled, "status": "updated"}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


from backend.app.api.customer_management import router as customer_management_router
from backend.app.api.customer_google_calendar import router as customer_google_calendar_router

router.include_router(customer_management_router)
router.include_router(customer_google_calendar_router)
