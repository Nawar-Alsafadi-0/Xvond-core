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
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.channels.models import AgentChannel


router = APIRouter(prefix="/customer/agents", tags=["Customer Agent Controls"])


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
