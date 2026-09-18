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
    self_service_channel_activation_blockers,
    self_service_readiness,
    self_service_spec_view,
)
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.channels.models import AgentChannel
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
            # Keep historical AutomationRun rows valid. A revised Job Brief
            # retires generated schedules instead of deleting their history.
            retired_config = dict(trigger_config)
            retired_config["_xvond_source"] = "self_service_employee_retired"
            retired_config["_xvond_retired_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            workflow.trigger_config = retired_config
            workflow.enabled = False


def _reconcile_builder_runtime_tools(
    db,
    *,
    agent_id: int,
    previous_capabilities: dict,
    next_capabilities: tuple[str, ...],
) -> None:
    """Keep Builder-owned tools aligned without overriding operator-managed tools."""
    previous = tuple(
        key for key, enabled in (previous_capabilities or {}).items() if enabled
    )
    previous_builder_tools = set(runtime_tools_for(previous))
    desired_tools = set(runtime_tools_for(next_capabilities))

    assignments = (
        db.query(AgentToolAssignment)
        .filter(AgentToolAssignment.agent_id == agent_id)
        .all()
    )
    by_name = {item.tool_name: item for item in assignments}

    for assignment in assignments:
        stored = dict(reveal_config(assignment.config) or {})
        builder_owned = (
            stored.get("_xvond_source") == "employee_builder"
            or (assignment.tool_name in previous_builder_tools and not stored)
        )
        if not builder_owned:
            continue

        if stored.get("_xvond_source") != "employee_builder":
            stored["_xvond_source"] = "employee_builder"

        if assignment.tool_name in desired_tools:
            if stored.pop("_xvond_retired_by_revision", None):
                assignment.enabled = True
            assignment.config = stored
            continue

        stored["_xvond_retired_by_revision"] = True
        assignment.config = stored
        assignment.enabled = False

    for tool_name in desired_tools:
        if tool_name in by_name:
            continue
        db.add(
            AgentToolAssignment(
                agent_id=agent_id,
                tool_name=tool_name,
                config={"_xvond_source": "employee_builder"},
                enabled=True,
            )
        )


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



def _builder_action(
    action_type: str,
    label: str,
    *,
    target: str | None = None,
    key: str | None = None,
    detail: str | None = None,
) -> dict:
    value = {"type": action_type, "label": label}
    if target:
        value["target"] = target
    if key:
        value["key"] = key
    if detail:
        value["detail"] = detail
    return value


def _self_service_builder_journey(
    *,
    agent: AIAgent,
    has_entitlement: bool,
    compiled_spec: dict | None,
    state: dict | None,
) -> dict:
    """Render the Self-Service lifecycle as structured product steps.

    Readiness remains the runtime authority. This view exists so the customer
    portal never has to parse human blocker strings to decide what the customer
    should do next.
    """

    state = dict(state or {})
    subscription = dict(state.get("subscription") or {})
    compiled = isinstance(compiled_spec, dict)
    provisioned = bool(
        compiled
        and (compiled_spec.get("delivery") or {}).get("provisioning_version") == 1
    )
    stages: list[dict] = []

    def add_stage(
        stage_id: str,
        label: str,
        status: str,
        detail: str,
        actions: list[dict] | None = None,
    ) -> None:
        stages.append(
            {
                "id": stage_id,
                "label": label,
                "status": status,
                "detail": detail,
                "actions": list(actions or []),
            }
        )

    add_stage(
        "brief",
        "Job Brief",
        "complete",
        "Your job description is saved as the source of truth for this employee.",
    )

    subscription_status = str(subscription.get("status") or "")
    if subscription.get("active"):
        add_stage(
            "plan",
            "Plan",
            "complete",
            f"{subscription.get('plan_name') or 'AI Employee plan'} is active.",
        )
    elif subscription_status == "pending_payment":
        add_stage(
            "plan",
            "Plan",
            "waiting",
            "The selected paid plan is waiting for payment or Xvond approval.",
        )
    else:
        add_stage(
            "plan",
            "Plan",
            "action_required",
            "Choose the AI Employee plan that will own runtime entitlement and limits.",
            [_builder_action("choose_plan", "Choose plan", target="subscription")],
        )

    if provisioned:
        add_stage(
            "build",
            "Build",
            "complete",
            "Xvond compiled the job and provisioned its employee capability plan.",
        )
    elif has_entitlement:
        add_stage(
            "build",
            "Build",
            "action_required",
            "Xvond is ready to compile this Job Brief into an executable employee plan.",
            [_builder_action("build_employee", "Build employee", target="builder")],
        )
    else:
        add_stage(
            "build",
            "Build",
            "blocked",
            "Activate an AI Employee plan before AI-backed compilation.",
        )

    setup_actions: list[dict] = []
    waiting_reasons: list[str] = []
    if compiled:
        missing_channels = {
            str(item or "").strip().lower()
            for item in (state.get("missing_channels") or [])
            if str(item or "").strip()
        }
        for channel in sorted(missing_channels):
            if channel == "website":
                setup_actions.append(
                    _builder_action(
                        "setup_website",
                        "Set up Website Chat",
                        target="agents",
                        key="website",
                    )
                )
            elif channel == "whatsapp":
                setup_actions.append(
                    _builder_action(
                        "setup_whatsapp",
                        "Connect WhatsApp",
                        target="agents",
                        key="whatsapp",
                    )
                )
            elif channel != "xvond":
                waiting_reasons.append(
                    f"{channel.replace('_', ' ').title()} needs an Xvond/provider adapter."
                )

        resolved = {
            str(item or "").strip().lower()
            for item in (state.get("resolved_requirements") or [])
        }
        for requirement in compiled_spec.get("requirements") or []:
            if not isinstance(requirement, dict):
                continue
            key = str(requirement.get("key") or "requirement").strip().lower()
            kind = str(requirement.get("kind") or "").strip().lower()
            status = str(requirement.get("status") or "").strip().lower()
            if status == "customer_input_required" and key not in resolved:
                if key in {"knowledge", "files"}:
                    if not any(item.get("type") == "manage_knowledge" for item in setup_actions):
                        setup_actions.append(
                            _builder_action(
                                "manage_knowledge",
                                "Add knowledge or files",
                                target="knowledge",
                                key=key,
                            )
                        )
                else:
                    setup_actions.append(
                        _builder_action(
                            "provide_input",
                            f"Provide {key.replace('_', ' ')}",
                            target="builder",
                            key=key,
                            detail=str(requirement.get("purpose") or "").strip() or None,
                        )
                    )
            elif status == "connection_required":
                if kind == "channel" and key in missing_channels:
                    continue
                connection_status = str(
                    requirement.get("self_service_connection_status") or ""
                )
                if connection_status == "xvond_adapter_required":
                    waiting_reasons.append(
                        f"{key.replace('_', ' ').title()} needs an Xvond connection adapter."
                    )
                elif kind != "channel":
                    waiting_reasons.append(
                        f"{key.replace('_', ' ').title()} connection is not customer-connectable yet."
                    )
            elif status == "xvond_managed":
                execution_status = str(requirement.get("execution_status") or "")
                schedule_status = str(requirement.get("schedule_status") or "")
                if execution_status not in {"", "ready"} or schedule_status not in {
                    "",
                    "ready",
                    "not_required",
                }:
                    waiting_reasons.append(
                        f"Xvond execution setup is still required for {key.replace('_', ' ')}."
                    )

        if state.get("provider_ready") is False:
            waiting_reasons.append(
                "Xvond must configure a real production AI provider/model route."
            )

        # De-duplicate while preserving the compiler/runtime order.
        deduped_actions: list[dict] = []
        seen_actions: set[tuple[str, str | None]] = set()
        for action in setup_actions:
            identity = (str(action.get("type")), action.get("key"))
            if identity in seen_actions:
                continue
            seen_actions.add(identity)
            deduped_actions.append(action)
        setup_actions = deduped_actions
        waiting_reasons = list(dict.fromkeys(waiting_reasons))

        if setup_actions:
            add_stage(
                "setup",
                "Setup",
                "action_required",
                "Finish only the customer inputs and connections this employee actually needs.",
                setup_actions,
            )
        elif waiting_reasons:
            add_stage(
                "setup",
                "Setup",
                "waiting",
                " ".join(waiting_reasons),
            )
        else:
            add_stage(
                "setup",
                "Setup",
                "complete",
                "All required customer inputs, connections and Xvond runtime setup are ready.",
            )
    else:
        add_stage(
            "setup",
            "Setup",
            "blocked",
            "Build the employee first so Xvond can determine the exact setup it needs.",
        )

    if agent.enabled:
        add_stage(
            "launch",
            "Launch",
            "complete",
            "This AI employee is live.",
        )
    elif state.get("ready"):
        add_stage(
            "launch",
            "Launch",
            "action_required",
            "All launch requirements are ready.",
            [_builder_action("launch_employee", "Launch employee", target="builder")],
        )
    else:
        add_stage(
            "launch",
            "Launch",
            "blocked",
            "Launch unlocks automatically when the required earlier steps are ready.",
        )

    next_actions: list[dict] = []
    for stage in stages:
        if stage["status"] == "action_required":
            next_actions = list(stage["actions"])
            break
        if stage["status"] in {"waiting", "blocked"}:
            break

    complete_count = sum(1 for stage in stages if stage["status"] == "complete")
    return {
        "stages": stages,
        "next_actions": next_actions,
        "complete_count": complete_count,
        "total_count": len(stages),
        "live": bool(agent.enabled),
    }


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
        builder_journey = None
        if is_self_service_company(company):
            compiled_spec = self_service_spec_view(compiled_spec)
            self_service_state = self_service_readiness(
                db,
                company=company,
                agent=agent,
                config=config,
            )
            builder_journey = _self_service_builder_journey(
                agent=agent,
                has_entitlement=has_entitlement,
                compiled_spec=compiled_spec if isinstance(compiled_spec, dict) else None,
                state=self_service_state,
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
                "builder_journey": builder_journey,
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
                    config={"_xvond_source": "employee_builder"},
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

        # Keep the same lock order as compilation: config first, then agent.
        # This serializes revision with launch/build without creating a lock cycle.
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        if agent.enabled:
            raise HTTPException(
                409,
                "Deactivate this employee before revising its Job Brief",
            )
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

        desired_channels = set(communication_channels(blueprint.channels))
        deactivated_channels = []
        channel_rows = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == company.id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.enabled.is_(True),
            )
            .with_for_update()
            .all()
        )
        for channel in channel_rows:
            channel_type = str(channel.channel_type or "").strip().lower()
            is_communication_channel = bool(communication_channels([channel_type]))
            if is_communication_channel and channel_type not in desired_channels:
                channel.enabled = False
                deactivated_channels.append(channel_type)

        _reconcile_builder_runtime_tools(
            db,
            agent_id=agent.id,
            previous_capabilities=dict(config.capabilities or {}),
            next_capabilities=blueprint.capabilities,
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

        db.commit()
        return {
            "status": "updated",
            "agent_id": agent.id,
            "compiled": False,
            "job_brief": blueprint.description,
            "requested_channels": list(communication_channels(blueprint.channels)),
            "deactivated_channels": deactivated_channels,
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
        config = _employee_config_or_404(db, agent)
        # Match compile/revise lock ordering to avoid config↔agent deadlocks.
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

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

        target_channel_types = [
            item for item in (state.get("slot_channels") or []) if item != "xvond"
        ]
        channel_rows = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == company.id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type.in_(target_channel_types),
            )
            .with_for_update()
            .all()
            if target_channel_types
            else []
        )
        channels_by_type = {
            str(row.channel_type or "").strip().lower(): row
            for row in channel_rows
        }
        missing_rows = [
            item for item in target_channel_types if item not in channels_by_type
        ]
        if missing_rows:
            raise HTTPException(
                409,
                detail={
                    "message": "Self-service communication channel setup is incomplete",
                    "blockers": [
                        f"Configure {item} before launch" for item in missing_rows
                    ],
                },
            )

        # Activate the employee and its selected communication surfaces in one
        # transaction. Channel readiness can now evaluate the live agent without
        # creating a Draft employee <-> inactive channel dependency cycle.
        agent.enabled = True
        company.active = True
        company.lifecycle_status = "live"
        company.lifecycle_updated_at = datetime.utcnow()
        db.flush()

        for channel_type in target_channel_types:
            channel = channels_by_type[channel_type]
            channel_blockers = self_service_channel_activation_blockers(
                db,
                company=company,
                agent=agent,
                channel=channel,
            )
            if channel_blockers:
                raise HTTPException(
                    409,
                    detail={
                        "message": f"{channel_type} is not ready for launch",
                        "blockers": channel_blockers,
                    },
                )
            if not channel.enabled:
                limits_service.check_channel_limit(db, company.id)
                channel.enabled = True
                db.flush()

        live = self_service_readiness(
            db,
            company=company,
            agent=agent,
            config=config,
        )
        if not live["ready"]:
            raise HTTPException(
                409,
                detail={
                    "message": "Self-service employee failed final launch readiness",
                    "blockers": live["blockers"],
                },
            )

        db.commit()
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
        config = _employee_config_or_404(db, agent)
        # Keep the same mutation lock order as launch/revision.
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

        channel_rows = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == company.id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.enabled.is_(True),
            )
            .with_for_update()
            .all()
        )
        deactivated_channels = []
        for channel in channel_rows:
            channel_type = str(channel.channel_type or "").strip().lower()
            if communication_channels([channel_type]):
                channel.enabled = False
                deactivated_channels.append(channel_type)

        agent.enabled = False
        company.active = False
        company.lifecycle_status = "paused"
        company.lifecycle_updated_at = datetime.utcnow()
        db.commit()
        return {
            "status": "draft",
            "agent_id": agent.id,
            "lifecycle": "draft",
            "company_lifecycle": "paused",
            "deactivated_channels": deactivated_channels,
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
