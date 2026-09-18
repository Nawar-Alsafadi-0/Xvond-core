from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from pydantic import BaseModel

from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager

from backend.app.models.company import Company
from backend.app.models.user import User

from backend.app.modules.ai_agent.customer_access import can_view_conversations
from backend.app.modules.ai_agent.models import (
    AIAgent,
    AIConversation,
    AIMessage,
)
from backend.app.modules.ai_agent.self_service_policy import (
    is_self_service_company,
    self_service_channel_slots,
)
from backend.app.modules.channels.conversation_source import bind_conversation_source

from backend.app.modules.ai_agent.factory_models import (
    AgentConfig,
)


router = APIRouter(
    prefix="/ai-agents",
    tags=["AI Agents"],
)


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


def require_conversation_access(
    db,
    current_user: User,
    agent_id: int,
):
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == current_user.company_id,
        )
        .first()
    )

    if agent is None:
        raise HTTPException(
            status_code=404,
            detail="AI Agent not found",
        )

    config = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id == agent.id)
        .first()
    )

    if not can_view_conversations(config):
        raise HTTPException(
            status_code=403,
            detail="Conversation viewing is disabled for this agent",
        )

    return agent


@router.get("/")
def list_agents(
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agents = (
            db.query(AIAgent)
            .filter(AIAgent.company_id == current_user.company_id)
            .order_by(AIAgent.id.asc())
            .all()
        )
        company = db.query(Company).filter(Company.id == current_user.company_id).first()
        self_service = is_self_service_company(company)
        configs_by_agent = {}
        if self_service and agents:
            configs_by_agent = {
                item.agent_id: item
                for item in (
                    db.query(AgentConfig)
                    .filter(AgentConfig.agent_id.in_([agent.id for agent in agents]))
                    .all()
                )
            }

        return {
            "company_id": current_user.company_id,
            "agents": [
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "enabled": item.enabled,
                    "self_service_channel_slots": (
                        self_service_channel_slots(
                            dict(
                                (
                                    configs_by_agent.get(item.id).settings
                                    if configs_by_agent.get(item.id) is not None
                                    else {}
                                )
                                or {}
                            ).get("employee_builder")
                        )
                        if self_service
                        else None
                    ),
                }
                for item in agents
            ],
        }
    finally:
        db.close()


@router.post("/{agent_id}/chat")
def chat(
    agent_id: int,
    data: ChatRequest,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        result = agent_runtime.chat(
            db=db,
            company_id=current_user.company_id,
            agent_id=agent_id,
            message=data.message,
            conversation_id=data.conversation_id,
            channel_type="portal_test",
            commit=False,
        )
        bind_conversation_source(
            db,
            conversation_id=result["conversation_id"],
            company_id=current_user.company_id,
            agent_id=agent_id,
            channel_type="portal_test",
        )
        db.commit()
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/{agent_id}/conversations")
def conversations(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        require_conversation_access(db, current_user, agent_id)
        items = (
            db.query(AIConversation)
            .filter(
                AIConversation.agent_id == agent_id,
                AIConversation.company_id == current_user.company_id,
            )
            .order_by(AIConversation.id.desc())
            .all()
        )
        return {
            "conversations": [
                {
                    "id": item.id,
                    "title": item.title,
                    "created_at": item.created_at,
                }
                for item in items
            ]
        }
    finally:
        db.close()


@router.get("/{agent_id}/conversations/{conversation_id}")
def conversation_messages(
    agent_id: int,
    conversation_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        require_conversation_access(db, current_user, agent_id)
        conversation = (
            db.query(AIConversation)
            .filter(
                AIConversation.id == conversation_id,
                AIConversation.agent_id == agent_id,
                AIConversation.company_id == current_user.company_id,
            )
            .first()
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = (
            db.query(AIMessage)
            .filter(AIMessage.conversation_id == conversation.id)
            .order_by(AIMessage.id.asc())
            .all()
        )
        return {
            "conversation_id": conversation.id,
            "messages": [
                {
                    "id": item.id,
                    "role": item.role,
                    "content": item.content,
                    "created_at": item.created_at,
                }
                for item in messages
            ],
        }
    finally:
        db.close()
