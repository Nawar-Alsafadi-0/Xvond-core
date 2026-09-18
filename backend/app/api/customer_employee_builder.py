from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.ai.engine import ProviderExecutionError, ai_engine
from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.company_lifecycle import portal_access_allowed
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.user import User
from backend.app.modules.ai_agent.employee_builder import (
    ACTION_CAPABILITIES,
    EmployeeBlueprint,
    blueprint_readiness,
    build_employee_blueprint,
    build_employee_system_prompt,
    missing_information_for,
    runtime_tools_for,
    sanitize_capabilities,
    sanitize_channels,
)
from backend.app.modules.ai_agent.employee_compiler import (
    COMPILER_SYSTEM_PROMPT,
    build_compiled_employee_system_prompt,
    build_compiler_user_message,
    parse_compiler_response,
)
from backend.app.modules.ai_agent.employee_capability_builder import provision_compiled_capabilities
from backend.app.modules.ai_agent.factory import agent_factory
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent, AIUsage
from backend.app.modules.ai_agent.profile_models import AIAgentProfile
from backend.app.modules.ai_agent.self_service_policy import (
    communication_channels,
    is_self_service_company,
    self_service_readiness,
)
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.providers.models import AIModelRecord, AIProviderRecord, CompanyAIProfile
from backend.app.modules.tools.models import AgentToolAssignment

router = APIRouter(
    prefix="/customer/employee-builder",
    tags=["Customer Employee Builder"],
)

SELF_SERVICE_FREE_TEST_MESSAGES = 0


class EmployeeBuilderCreateRequest(BaseModel):
    description: str = Field(min_length=8, max_length=4000)
    name: str | None = Field(default=None, max_length=200)
    capabilities: list[str] | None = None
    channels: list[str] | None = None


class EmployeeBuilderTestRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)


class EmployeeBuilderReviseRequest(BaseModel):
    description: str = Field(min_length=8, max_length=4000)


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


def _company_or_404(db, company_id: int) -> Company:
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None or not portal_access_allowed(company):
        raise HTTPException(404, "Company workspace not found")
    return company


def _has_ai_agents_entitlement(db, company_id: int) -> bool:
    try:
        service_limits.entitlement(db, company_id, "ai_agents")
        return True
    except HTTPException as exc:
        if exc.status_code == 403:
            return False
        raise


def _existing_employee(db, company_id: int) -> AIAgent | None:
    return (
        db.query(AIAgent)
        .join(AgentConfig, AgentConfig.agent_id == AIAgent.id)
        .filter(
            AIAgent.company_id == company_id,
            AgentConfig.agent_type == "employee",
        )
        .order_by(AIAgent.id.asc())
        .first()
    )


def _employee_config_or_404(db, agent: AIAgent) -> AgentConfig:
    config = db.query(AgentConfig).filter(AgentConfig.agent_id == agent.id).first()
    if config is None or config.agent_type != "employee":
        raise HTTPException(404, "Employee Builder employee not found")
    return config


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
        capability: ("ask_before_action" if capability in ACTION_CAPABILITIES else "automatic")
        for capability in capabilities
    }

    return EmployeeBlueprint(
        name=(data.name or base.name).strip() or base.name,
        description=base.description,
        audience=base.audience,
        capabilities=capabilities,
        channels=channels,
        permissions=permissions,
        missing_information=missing_information_for(capabilities, channels),
    )


def _record_ai_usage(db, *, company_id: int, agent_id: int, selected, response):
    db.add(
        AIUsage(
            company_id=company_id,
            agent_id=agent_id,
            provider=selected.provider,
            model=selected.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.total_tokens,
            provider_cost=response.cost,
            status="success",
            latency_ms=0,
        )
    )


def _store_provisioned_spec(
    db, *, company_id: int, agent: AIAgent, config: AgentConfig,
    settings: dict, builder: dict, spec: dict,
) -> dict:
    compiled_spec, delivery = provision_compiled_capabilities(db, agent_id=agent.id, spec=spec)
    builder["compiled_spec"] = compiled_spec
    builder["delivery"] = delivery
    builder["missing_information"] = list(compiled_spec.get("setup_required") or [])
    settings["employee_builder"] = builder
    config.settings = settings
    company = db.query(Company).filter(Company.id == company_id).first()
    agent.system_prompt = build_compiled_employee_system_prompt(
        owner_name=company.name if company else "the owner",
        spec=compiled_spec,
    )
    # Flush the spec and contracts together; the caller owns commit/rollback.
    db.flush()
    return compiled_spec


def _clear_generated_self_service_build(db, *, company_id: int, agent_id: int) -> None:
    """Remove only runtime artifacts generated from the previous Job Brief."""
    assignment = (
        db.query(AgentToolAssignment)
        .filter(
            AgentToolAssignment.agent_id == agent_id,
            AgentToolAssignment.tool_name == "action_request",
        )
        .first()
    )
    if assignment is not None:
        assignment_config = dict(reveal_config(assignment.config) or {})
        actions = dict(assignment_config.get("actions") or {})
        kept_actions = {
            key: value
            for key, value in actions.items()
            if not (isinstance(value, dict) and value.get("xvond_generated"))
        }
        if kept_actions != actions:
            assignment_config["actions"] = kept_actions
            assignment.config = assignment_config

    workflows = (
        db.query(AutomationWorkflow)
        .filter(
            AutomationWorkflow.company_id == company_id,
            AutomationWorkflow.trigger_type == "schedule",
        )
        .all()
    )
    for workflow in workflows:
        trigger_config = workflow.trigger_config if isinstance(workflow.trigger_config, dict) else {}
        if (
            trigger_config.get("_xvond_source") == "self_service_employee"
            and int(trigger_config.get("_xvond_agent_id") or 0) == int(agent_id)
        ):
            db.delete(workflow)


def _compile_employee_spec(db, *, company_id: int, agent: AIAgent, config: AgentConfig) -> dict:
    # Serialize compilation/provisioning, including retries of a cached spec.
    db.refresh(config, with_for_update=True)
    settings = dict(config.settings or {})
    builder = dict(settings.get("employee_builder") or {})
    existing = builder.get("compiled_spec")
    if isinstance(existing, dict) and existing.get("job_brief"):
        return _store_provisioned_spec(
            db, company_id=company_id, agent=agent, config=config,
            settings=settings, builder=builder, spec=existing,
        )

    job_brief = str(builder.get("source_description") or agent.description or "").strip()
    if not job_brief:
        raise HTTPException(400, "Employee job brief is missing")
    requested_channels = list(builder.get("requested_channels") or [])
    company = db.query(Company).filter(Company.id == company_id).first()
    if is_self_service_company(company):
        requested_channels = communication_channels(requested_channels)

    selections = runtime_selections(
        db,
        company_id,
        agent.provider,
        agent.model,
        message=job_brief,
    )
    if not selections:
        raise HTTPException(503, "No eligible AI provider/model is available")

    response = None
    selected = None
    compiled_spec = None
    for candidate in selections:
        try:
            candidate_response = ai_engine.generate(
                provider_name=candidate.provider,
                system_prompt=COMPILER_SYSTEM_PROMPT,
                user_message=build_compiler_user_message(
                    job_brief=job_brief,
                    requested_channels=requested_channels,
                ),
                model=candidate.model,
                tools=None,
            )
            candidate_spec = parse_compiler_response(
                candidate_response.text,
                job_brief=job_brief,
            )
            response = candidate_response
            selected = candidate
            compiled_spec = candidate_spec
            break
        except (ProviderExecutionError, ValueError):
            continue

    if response is None or selected is None or compiled_spec is None:
        raise HTTPException(502, "AI employee compiler could not produce a valid specification")

    builder["compiled_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    builder["compiler_provider"] = selected.provider
    builder["compiler_model"] = selected.model
    compiled_spec = _store_provisioned_spec(
        db, company_id=company_id, agent=agent, config=config,
        settings=settings, builder=builder, spec=compiled_spec,
    )

    _record_ai_usage(
        db,
        company_id=company_id,
        agent_id=agent.id,
        selected=selected,
        response=response,
    )
    return compiled_spec


@router.get("/current")
def current_employee(current_user: User = Depends(require_customer_manager)):
    db = SessionLocal()
    try:
        agent = _existing_employee(db, current_user.company_id)
        if agent is None:
            return {"employee": None}
        config = _employee_config_or_404(db, agent)
        builder = (config.settings or {}).get("employee_builder") or {}
        compiled_spec = builder.get("compiled_spec") if isinstance(builder, dict) else None
        has_entitlement = _has_ai_agents_entitlement(db, current_user.company_id)
        company = _company_or_404(db, current_user.company_id)
        self_service_state = None
        if is_self_service_company(company):
            self_service_state = self_service_readiness(
                db,
                company=company,
                agent=agent,
                config=config,
            )
        display_channels = list(builder.get("requested_channels", []))
        if is_self_service_company(company):
            display_channels = communication_channels(display_channels)
        return {
            "employee": {
                "agent_id": agent.id,
                "name": agent.name,
                "description": agent.description,
                "enabled": agent.enabled,
                "lifecycle": "live" if agent.enabled else "draft",
                "capabilities": [key for key, value in (config.capabilities or {}).items() if value],
                "requested_channels": display_channels,
                "permissions": builder.get("permissions", {}),
                "missing_information": builder.get("missing_information", []),
                "compiled": isinstance(compiled_spec, dict),
                "compiled_spec": compiled_spec if isinstance(compiled_spec, dict) else None,
                "can_compile": has_entitlement,
                "delivery_mode": (
                    "self_service"
                    if is_self_service_company(company)
                    else "managed"
                ),
                "self_service_readiness": self_service_state,
                "can_launch": bool(
                    self_service_state
                    and self_service_state.get("ready")
                    and not agent.enabled
                ),
            }
        }
    finally:
        db.close()


@router.post("/create")
def create_employee(
    data: EmployeeBuilderCreateRequest,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        has_entitlement = _has_ai_agents_entitlement(db, company.id)
        is_self_service = str(company.onboarding_source or "managed") == "self_service"
        if not has_entitlement and not is_self_service:
            service_limits.entitlement(db, company.id, "ai_agents")

        existing = _existing_employee(db, company.id)
        if existing is not None:
            raise HTTPException(
                409,
                detail={
                    "message": "This workspace already has an AI employee",
                    "agent_id": existing.id,
                },
            )

        try:
            blueprint = _build_final_blueprint(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        for module_name in ("ai_agent", "knowledge", "tools"):
            _ensure_module(db, company.id, module_name)

        provider, model = _select_model(db, company.id)
        requested_channels = (
            communication_channels(blueprint.channels)
            if is_self_service
            else list(blueprint.channels)
        )
        settings = {
            "employee_builder": {
                "version": 2,
                "source_description": blueprint.description,
                "job_brief": blueprint.description,
                "audience": blueprint.audience,
                "requested_channels": requested_channels,
                "permissions": dict(blueprint.permissions),
                "missing_information": list(blueprint.missing_information),
                "onboarding_source": company.onboarding_source,
                "delivery_mode": (
                    "self_service"
                    if is_self_service
                    else "managed"
                ),
                "compiled_spec": None,
            },
            "dialect": "auto",
            "response_length": "concise",
            "clarification_style": "smart",
            "off_topic_behavior": "brief_friendly" if blueprint.audience == "personal" else "business_redirect",
        }

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
            capabilities={item: True for item in blueprint.capabilities},
            customer_controls=dict(DEFAULT_CUSTOMER_CONTROLS),
            enforce_capacity=has_entitlement,
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

        db.commit()
        db.refresh(agent)
        config = db.query(AgentConfig).filter(AgentConfig.agent_id == agent.id).first()
        return {
            "status": "created",
            "lifecycle": "draft",
            "agent_id": agent.id,
            "name": agent.name,
            "enabled": agent.enabled,
            "subscription_required_for_go_live": not has_entitlement,
            "subscription_required_for_compile": not has_entitlement,
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


@router.patch("/{agent_id}/job-brief")
def revise_self_service_job_brief(
    agent_id: int,
    data: EmployeeBuilderReviseRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Revise a draft Self-Service Job Brief and invalidate only generated build artifacts."""
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(
                409,
                "Xvond Managed employees must use the managed delivery flow",
            )

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == agent_id,
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        # Serialize revision with launch so a stale readiness snapshot cannot
        # enable an employee while its generated build artifacts are changing.
        db.refresh(agent, with_for_update=True)
        if agent.enabled:
            raise HTTPException(
                409,
                "Deactivate this employee before revising its Job Brief",
            )

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        try:
            blueprint = _build_final_blueprint(
                EmployeeBuilderCreateRequest(
                    description=data.description,
                    name=agent.name,
                )
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        settings = dict(config.settings or {})
        builder = dict(settings.get("employee_builder") or {})
        builder.update(
            {
                "version": 2,
                "source_description": blueprint.description,
                "job_brief": blueprint.description,
                "audience": blueprint.audience,
                "requested_channels": communication_channels(blueprint.channels),
                "permissions": dict(blueprint.permissions),
                "missing_information": list(blueprint.missing_information),
                "onboarding_source": company.onboarding_source,
                "delivery_mode": "self_service",
                "compiled_spec": None,
            }
        )
        for stale_key in (
            "delivery",
            "compiled_at",
            "compiler_provider",
            "compiler_model",
        ):
            builder.pop(stale_key, None)
        settings["employee_builder"] = builder

        _clear_generated_self_service_build(
            db,
            company_id=company.id,
            agent_id=agent.id,
        )

        config.settings = settings
        config.capabilities = {item: True for item in blueprint.capabilities}
        agent.description = blueprint.description
        agent.system_prompt = build_employee_system_prompt(
            owner_name=company.name,
            blueprint=blueprint,
        )

        profile = (
            db.query(AIAgentProfile)
            .filter(
                AIAgentProfile.company_id == company.id,
                AIAgentProfile.agent_id == agent.id,
            )
            .first()
        )
        if profile is not None:
            profile.instructions = blueprint.description
            profile.business_type = "personal" if blueprint.audience == "personal" else None

        existing_tools = {
            row.tool_name
            for row in db.query(AgentToolAssignment).filter(
                AgentToolAssignment.agent_id == agent.id
            )
        }
        for tool_name in runtime_tools_for(blueprint.capabilities):
            if tool_name not in existing_tools:
                db.add(
                    AgentToolAssignment(
                        agent_id=agent.id,
                        tool_name=tool_name,
                        config={},
                        enabled=True,
                    )
                )

        db.commit()
        return {
            "status": "updated",
            "agent_id": agent.id,
            "compiled": False,
            "job_brief": blueprint.description,
            "requested_channels": list(communication_channels(blueprint.channels)),
            "missing_information": list(blueprint.missing_information),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/compile")
def compile_employee(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    """Turn an open-ended paid Job Brief into a structured employee specification."""
    db = SessionLocal()
    try:
        _company_or_404(db, current_user.company_id)
        service_limits.entitlement(db, current_user.company_id, "ai_agents")
        limits_service.check_token_limit(db, current_user.company_id)

        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == current_user.company_id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)

        compiled_spec = _compile_employee_spec(
            db,
            company_id=current_user.company_id,
            agent=agent,
            config=config,
        )
        db.commit()
        return {
            "agent_id": agent.id,
            "compiled": True,
            "spec": compiled_spec,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/{agent_id}/readiness")
def self_service_employee_readiness(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        return self_service_readiness(
            db,
            company=company,
            agent=agent,
            config=config,
        )
    finally:
        db.close()


@router.post("/{agent_id}/launch")
def launch_self_service_employee(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    """Launch only a self-service employee; Managed employees keep their existing admin flow."""
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(
                409,
                "Xvond Managed employees must use the managed delivery flow",
            )

        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        db.refresh(agent, with_for_update=True)
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)

        state = self_service_readiness(
            db,
            company=company,
            agent=agent,
            config=config,
        )
        if not state["ready"]:
            raise HTTPException(
                409,
                detail={
                    "message": "Self-service employee is not ready to launch",
                    "blockers": state["blockers"],
                },
            )

        # Existing AI Agents plan still owns employee capacity. The self-service
        # workspace currently owns one employee, so its active subscription is
        # the commercial entitlement for this employee.
        limits_service.check_agent_limit(db, company.id)
        agent.enabled = True
        company.active = True
        company.lifecycle_status = "live"
        company.lifecycle_updated_at = datetime.utcnow()
        db.commit()

        live = self_service_readiness(
            db,
            company=company,
            agent=agent,
            config=config,
        )
        return {
            **live,
            "status": "live",
            "agent_id": agent.id,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/deactivate")
def deactivate_self_service_employee(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(
                409,
                "Xvond Managed employees must use the managed delivery flow",
            )
        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        _employee_config_or_404(db, agent)
        agent.enabled = False
        db.commit()
        return {
            "status": "draft",
            "agent_id": agent.id,
            "lifecycle": "draft",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/test")
def test_draft_employee(
    agent_id: int,
    data: EmployeeBuilderTestRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Test the employee safely without enabling channels or executing tools."""
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == current_user.company_id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        config = _employee_config_or_404(db, agent)

        has_entitlement = _has_ai_agents_entitlement(db, current_user.company_id)
        is_self_service = str(company.onboarding_source or "managed") == "self_service"
        free_tests_remaining = None
        if has_entitlement:
            limits_service.check_token_limit(db, current_user.company_id)
            _compile_employee_spec(
                db,
                company_id=current_user.company_id,
                agent=agent,
                config=config,
            )
        elif is_self_service:
            used = db.query(AIUsage).filter(
                AIUsage.company_id == current_user.company_id,
                AIUsage.agent_id == agent.id,
                AIUsage.status == "success",
            ).count()
            if used >= SELF_SERVICE_FREE_TEST_MESSAGES:
                raise HTTPException(
                    403,
                    detail={
                        "message": "Subscribe to launch and test your AI employee",
                        "subscription_required": True,
                    },
                )
            free_tests_remaining = SELF_SERVICE_FREE_TEST_MESSAGES - used - 1
        else:
            service_limits.entitlement(db, current_user.company_id, "ai_agents")

        selections = runtime_selections(
            db,
            current_user.company_id,
            agent.provider,
            agent.model,
            message=data.message,
        )
        if not selections:
            raise HTTPException(503, "No eligible AI provider/model is available")

        response = None
        selected = None
        for candidate in selections:
            try:
                response = ai_engine.generate(
                    provider_name=candidate.provider,
                    system_prompt=agent.system_prompt,
                    user_message=data.message,
                    model=candidate.model,
                    tools=None,
                )
                selected = candidate
                break
            except ProviderExecutionError:
                continue

        if response is None or selected is None:
            raise HTTPException(503, "AI provider is temporarily unavailable")

        _record_ai_usage(
            db,
            company_id=current_user.company_id,
            agent_id=agent.id,
            selected=selected,
            response=response,
        )
        db.commit()
        return {
            "agent_id": agent.id,
            "lifecycle": "live" if agent.enabled else "draft",
            "message": response.text,
            "usage": {
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "total_tokens": response.total_tokens,
            },
            "free_tests_remaining": free_tests_remaining,
            "tools_used": False,
            "channels_used": False,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
