from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.user import User
from backend.app.modules.ai_agent.employee_builder import (
    EmployeeBlueprint,
    blueprint_readiness,
    build_employee_blueprint,
    build_employee_system_prompt,
    runtime_channels_for,
    runtime_tools_for,
    sanitize_capabilities,
    sanitize_channels,
)
from backend.app.modules.ai_agent.factory import agent_factory
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.profile_models import AIAgentProfile
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.providers.models import (
    AIModelRecord,
    AIProviderRecord,
    CompanyAIProfile,
)
from backend.app.modules.tools.models import AgentToolAssignment


router = APIRouter(
    prefix="/customer/employee-builder",
    tags=["Customer Employee Builder"],
)


class EmployeeBuilderPreviewRequest(BaseModel):
    description: str = Field(min_length=8, max_length=4000)


class EmployeeBuilderCreateRequest(EmployeeBuilderPreviewRequest):
    name: str | None = Field(default=None, max_length=200)
    capabilities: list[str] | None = None
    channels: list[str] | None = None


DEFAULT_CUSTOMER_CONTROLS = {
    "can_enable_disable": True,
    "can_view_conversations": True,
    "can_view_usage": True,
    "can_edit_prompt": True,
    "can_change_provider": False,
    "can_change_model": False,
}


def _ensure_module(db, company_id: int, name: str):
    row = db.query(CompanyModule).filter(
        CompanyModule.company_id == company_id,
        CompanyModule.module_name == name,
    ).first()
    if row is None:
        row = CompanyModule(company_id=company_id, module_name=name, enabled=True)
        db.add(row)
    else:
        row.enabled = True
    return row


def _select_model(db, company_id: int) -> tuple[str, str]:
    profile = db.query(CompanyAIProfile).filter(
        CompanyAIProfile.company_id == company_id
    ).first()
    if profile and profile.default_provider and profile.default_model:
        valid = (
            db.query(AIModelRecord)
            .join(AIProviderRecord, AIProviderRecord.name == AIModelRecord.provider_name)
            .filter(
                AIModelRecord.provider_name == profile.default_provider,
                AIModelRecord.model_name == profile.default_model,
                AIModelRecord.enabled.is_(True),
                AIProviderRecord.enabled.is_(True),
            )
            .first()
        )
        if valid is not None:
            return profile.default_provider, profile.default_model

    row = (
        db.query(AIModelRecord, AIProviderRecord)
        .join(AIProviderRecord, AIProviderRecord.name == AIModelRecord.provider_name)
        .filter(
            AIModelRecord.enabled.is_(True),
            AIProviderRecord.enabled.is_(True),
        )
        .order_by(AIProviderRecord.priority.asc(), AIModelRecord.id.asc())
        .first()
    )
    if not row:
        raise HTTPException(400, "No enabled AI provider/model is configured")
    model, provider = row
    return provider.name, model.model_name


def _build_final_blueprint(data: EmployeeBuilderCreateRequest) -> EmployeeBlueprint:
    base = build_employee_blueprint(data.description)
    capabilities = sanitize_capabilities(data.capabilities, base.capabilities)
    channels = sanitize_channels(data.channels, base.channels)

    permissions = {
        capability: base.permissions.get(capability, "automatic")
        for capability in capabilities
    }
    action_capabilities = {"sales", "lead_capture", "booking", "orders", "email", "scheduling"}
    for capability in capabilities:
        if capability in action_capabilities:
            permissions[capability] = "ask_before_action"

    missing = list(base.missing_information)
    for channel in channels:
        marker = f"connect_{channel}"
        if channel not in {"xvond", "website"} and marker not in missing:
            missing.append(marker)

    return EmployeeBlueprint(
        name=(data.name or base.name).strip() or base.name,
        description=base.description,
        audience=base.audience,
        capabilities=capabilities,
        channels=channels,
        permissions=permissions,
        missing_information=tuple(dict.fromkeys(missing)),
    )


@router.post("/preview")
def preview_employee(
    data: EmployeeBuilderPreviewRequest,
    current_user: User = Depends(require_customer_manager),
):
    try:
        blueprint = build_employee_blueprint(data.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "company_id": current_user.company_id,
        "blueprint": blueprint.as_dict(),
        "readiness": blueprint_readiness(blueprint),
        "lifecycle": "preview",
    }


@router.post("/create")
def create_employee(
    data: EmployeeBuilderCreateRequest,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = db.query(Company).filter(
            Company.id == current_user.company_id,
            Company.active.is_(True),
        ).first()
        if company is None:
            raise HTTPException(404, "Company workspace not found")

        try:
            blueprint = _build_final_blueprint(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        for module_name in ("ai_agent", "knowledge", "tools"):
            _ensure_module(db, company.id, module_name)

        provider, model = _select_model(db, company.id)
        settings = {
            "employee_builder": {
                "version": 1,
                "source_description": blueprint.description,
                "audience": blueprint.audience,
                "requested_channels": list(blueprint.channels),
                "permissions": dict(blueprint.permissions),
                "missing_information": list(blueprint.missing_information),
            },
            "dialect": "auto",
            "response_length": "concise",
            "clarification_style": "smart",
            "off_topic_behavior": "brief_friendly" if blueprint.audience == "personal" else "business_redirect",
        }
        capability_flags = {item: True for item in blueprint.capabilities}

        agent = agent_factory.create_custom_agent(
            db=db,
            company_id=company.id,
            name=blueprint.name,
            description=blueprint.description,
            system_prompt=build_employee_system_prompt(owner_name=company.name, blueprint=blueprint),
            provider=provider,
            model=model,
            agent_type="employee",
            settings=settings,
            capabilities=capability_flags,
            customer_controls=dict(DEFAULT_CUSTOMER_CONTROLS),
        )

        db.add(
            AIAgentProfile(
                company_id=company.id,
                agent_id=agent.id,
                business_name=company.name,
                business_type="personal" if blueprint.audience == "personal" else None,
                reply_language="auto",
                conversation_style="professional_friendly",
                greeting=None,
                instructions=blueprint.description,
            )
        )

        for tool_name in runtime_tools_for(blueprint.capabilities):
            db.add(
                AgentToolAssignment(
                    agent_id=agent.id,
                    tool_name=tool_name,
                    config={},
                    enabled=True,
                )
            )

        for channel_type in runtime_channels_for(blueprint.channels):
            db.add(
                AgentChannel(
                    company_id=company.id,
                    agent_id=agent.id,
                    channel_type=channel_type,
                    config={"created_by": "employee_builder_v1"},
                    enabled=False,
                )
            )

        db.commit()
        db.refresh(agent)
        config = db.query(AgentConfig).filter(AgentConfig.agent_id == agent.id).first()
        return {
            "status": "created",
            "lifecycle": "draft",
            "agent_id": agent.id,
            "name": agent.name,
            "enabled": agent.enabled,
            "blueprint": blueprint.as_dict(),
            "readiness": blueprint_readiness(blueprint),
            "config_id": config.id if config else None,
        }
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
