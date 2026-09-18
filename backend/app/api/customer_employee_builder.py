from copy import deepcopy
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.ai.engine import ProviderExecutionError, ai_engine
from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.company_lifecycle import portal_access_allowed
from backend.app.core.config_secrets import reveal_config
from backend.app.core.config.settings import settings
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
    is_sensitive_requirement_key,
    normalize_requirement_key,
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
    self_service_channel_slots,
    self_service_connection_status,
    self_service_readiness,
    self_service_spec_view,
)
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime
from backend.app.modules.automation.webhook_auth import automation_webhook_key
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_INTERNAL,
    CHANNEL_SETUP_MANAGED,
    CHANNEL_SETUP_SELF_SERVICE,
    canonical_channel_type,
    get_channel_capability,
)
from backend.app.modules.channels.delivery import reconcile_managed_channel_requests
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.providers.models import AIModelRecord, AIProviderRecord, CompanyAIProfile
from backend.app.modules.tools.models import AgentToolAssignment
from backend.app.modules.tools.business_models import ActionRequest
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.integrations.catalog import integration_validation_ready

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
    description: str = Field(min_length=8, max_length=12000)


class EmployeeBuilderRefineRequest(BaseModel):
    instruction: str = Field(min_length=2, max_length=2000)


class EmployeeBuilderRollbackRequest(BaseModel):
    version_id: str = Field(min_length=1, max_length=80)


class EmployeeBuilderIntegrationBindRequest(BaseModel):
    integration_id: int
    execute_endpoint: str | None = Field(default=None, max_length=500)
    availability_endpoint: str | None = Field(default=None, max_length=500)
    cancel_endpoint: str | None = Field(default=None, max_length=500)


class EmployeeBuilderSetupAnswerRequest(BaseModel):
    value: str | None = Field(default=None, max_length=8000)
    values: dict[str, str] = Field(default_factory=dict)

class EmployeeBuilderGraphRunRequest(BaseModel):
    input_data: dict = Field(default_factory=dict)


DEFAULT_CUSTOMER_CONTROLS = {
    "can_enable_disable": True,
    "can_view_conversations": True,
    "can_view_usage": True,
    "can_edit_prompt": True,
    "can_change_provider": False,
    "can_change_model": False,
}


BUILDER_HISTORY_LIMIT = 20


def _snapshot_builder_version(
    builder: dict,
    *,
    reason: str,
    capabilities: dict | None = None,
) -> dict:
    history = list(builder.get("versions") or [])
    snapshot_source = str(builder.get("source_description") or "").strip()
    snapshot_spec = builder.get("compiled_spec")
    if not snapshot_source and not isinstance(snapshot_spec, dict):
        return builder

    version_id = (
        datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        + f"-{len(history) + 1}"
    )
    history.append(
        {
            "id": version_id,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "reason": str(reason or "change")[:120],
            "source_description": snapshot_source[:12000],
            "compiled_spec": deepcopy(snapshot_spec) if isinstance(snapshot_spec, dict) else None,
            "compiled_at": builder.get("compiled_at"),
            "setup_answers": deepcopy(dict(builder.get("setup_answers") or {})),
            "requested_channels": list(builder.get("requested_channels") or []),
            "audience": builder.get("audience"),
            "permissions": deepcopy(dict(builder.get("permissions") or {})),
            "capabilities": dict(capabilities or {}),
        }
    )
    builder["versions"] = history[-BUILDER_HISTORY_LIMIT:]
    return builder


def _clear_current_build_evidence(builder: dict) -> None:
    for key in (
        "compiled_spec",
        "delivery",
        "compiled_at",
        "compiler_provider",
        "compiler_model",
        "last_tested_at",
        "last_tested_compiled_at",
    ):
        builder.pop(key, None)
    # Keep the serialized builder contract explicit for callers and existing
    # stored workspaces: a revised/rolled-back draft is uncompiled, rather than
    # having an ambiguous missing compilation field.
    builder["compiled_spec"] = None


def _builder_versions_view(builder: dict) -> list[dict]:
    result = []
    for item in reversed(list(builder.get("versions") or [])):
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "id": item.get("id"),
                "created_at": item.get("created_at"),
                "reason": item.get("reason"),
                "job_brief": item.get("source_description"),
                "compiled": isinstance(item.get("compiled_spec"), dict),
                "compiled_at": item.get("compiled_at"),
            }
        )
    return result


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
    setup_answers = builder.get("setup_answers") or {}
    if isinstance(setup_answers, dict):
        clean_answers: dict[str, str | dict] = {}
        for raw_key, raw_value in setup_answers.items():
            key = str(raw_key or "").strip().lower()
            if not key:
                continue
            if isinstance(raw_value, dict):
                fields = {
                    str(field_key or "").strip().lower(): str(field_value or "").strip()
                    for field_key, field_value in raw_value.items()
                    if str(field_key or "").strip() and str(field_value or "").strip()
                }
                if fields:
                    clean_answers[key] = fields
            else:
                value = str(raw_value or "").strip()
                if value:
                    clean_answers[key] = value
        if clean_answers:
            compiled_spec = dict(compiled_spec)
            compiled_spec["customer_inputs"] = clean_answers
    builder["compiled_spec"] = compiled_spec
    builder["delivery"] = delivery
    builder["missing_information"] = list(compiled_spec.get("setup_required") or [])
    settings["employee_builder"] = builder
    config.settings = settings
    company = db.query(Company).filter(Company.id == company_id).first()
    if is_self_service_company(company):
        reconcile_managed_channel_requests(
            db,
            company_id=company_id,
            agent_id=agent.id,
            desired_channel_types=self_service_channel_slots(builder),
            request_source="compiled_employee_contract",
        )
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
        .filter(AutomationWorkflow.company_id == company_id)
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


def _carry_forward_requirement_bindings(previous_spec: dict | None, next_spec: dict) -> dict:
    previous = {
        normalize_requirement_key(item.get("key")): item
        for item in ((previous_spec or {}).get("requirements") or [])
        if isinstance(item, dict) and normalize_requirement_key(item.get("key"))
    }
    updated = deepcopy(next_spec)
    requirements = []
    carry_fields = {
        "integration_id",
        "integration_type",
        "integration_operations",
        "fulfillment_mode",
        "validation_required",
        "requires_connection",
    }
    for raw in updated.get("requirements") or []:
        if not isinstance(raw, dict):
            requirements.append(raw)
            continue
        item = dict(raw)
        prior = previous.get(normalize_requirement_key(item.get("key")))
        if isinstance(prior, dict):
            for field in carry_fields:
                if field in prior and field not in item:
                    item[field] = deepcopy(prior[field])
        requirements.append(item)
    updated["requirements"] = requirements
    return updated


def _compile_staged_employee_spec(
    db,
    *,
    company_id: int,
    agent: AIAgent,
    job_brief: str,
    requested_channels: list[str],
    previous_spec: dict | None,
) -> dict:
    selections = runtime_selections(
        db,
        company_id,
        agent.provider,
        agent.model,
        message=job_brief,
    )
    if not selections:
        raise HTTPException(503, "No eligible AI provider/model is available")

    for candidate in selections:
        try:
            response = ai_engine.generate(
                provider_name=candidate.provider,
                system_prompt=COMPILER_SYSTEM_PROMPT,
                user_message=build_compiler_user_message(
                    job_brief=job_brief,
                    requested_channels=requested_channels,
                ),
                model=candidate.model,
                tools=None,
            )
            spec = parse_compiler_response(response.text, job_brief=job_brief)
            spec = _carry_forward_requirement_bindings(previous_spec, spec)
            _record_ai_usage(
                db,
                company_id=company_id,
                agent_id=agent.id,
                selected=candidate,
                response=response,
            )
            return {
                "compiled_spec": spec,
                "compiled_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "compiler_provider": candidate.provider,
                "compiler_model": candidate.model,
            }
        except (ProviderExecutionError, ValueError):
            continue
    raise HTTPException(502, "AI employee compiler could not produce a valid staged specification")



def _builder_action(
    action_type: str,
    label: str,
    *,
    target: str | None = None,
    key: str | None = None,
    detail: str | None = None,
    fields: list[dict] | None = None,
) -> dict:
    value = {"type": action_type, "label": label}
    if target:
        value["target"] = target
    if key:
        value["key"] = key
    if detail:
        value["detail"] = detail
    if fields:
        value["fields"] = list(fields)
    return value


def _self_service_builder_journey(
    *,
    agent: AIAgent,
    has_entitlement: bool,
    compiled_spec: dict | None,
    state: dict | None,
    builder: dict | None = None,
) -> dict:
    """Render the Self-Service lifecycle as structured product steps.

    Readiness remains the runtime authority. This view exists so the customer
    portal never has to parse human blocker strings to decide what the customer
    should do next.
    """

    state = dict(state or {})
    builder = dict(builder or {})
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
            "Payment pending. The selected paid plan is waiting for payment or Xvond approval.",
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
            canonical_channel_type(item)
            for item in (state.get("missing_channels") or [])
            if canonical_channel_type(item)
        }
        for channel in sorted(missing_channels):
            capability = get_channel_capability(channel) or {}
            channel_name = str(capability.get("name") or channel.replace("_", " ").title())
            setup_mode = capability.get("setup_mode")
            runtime_state = capability.get("runtime_state")

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
            elif setup_mode == CHANNEL_SETUP_INTERNAL:
                continue
            elif runtime_state == CHANNEL_RUNTIME_LIVE and setup_mode == CHANNEL_SETUP_MANAGED:
                waiting_reasons.append(
                    f"{channel_name}: Xvond managed provisioning is required before launch."
                )
            else:
                waiting_reasons.append(
                    f"{channel_name}: requested as an Xvond-managed channel; a runtime adapter must be provisioned before launch."
                )

        resolved = {
            str(item or "").strip().lower()
            for item in (state.get("resolved_requirements") or [])
        }
        for requirement in compiled_spec.get("requirements") or []:
            if not isinstance(requirement, dict):
                continue
            key = (
                canonical_channel_type(requirement.get("key"))
                if str(requirement.get("kind") or "").strip().lower() == "channel"
                else str(requirement.get("key") or "requirement").strip().lower()
            )
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
                    input_fields: list[dict] = []
                    input_labels = (
                        requirement.get("customer_input_labels")
                        if isinstance(requirement.get("customer_input_labels"), dict)
                        else {}
                    )
                    input_purposes = (
                        requirement.get("customer_input_purposes")
                        if isinstance(requirement.get("customer_input_purposes"), dict)
                        else {}
                    )
                    sensitive_input = is_sensitive_requirement_key(key)
                    for raw_field in requirement.get("customer_inputs") or []:
                        field_key = normalize_requirement_key(raw_field)
                        if not field_key:
                            continue
                        if is_sensitive_requirement_key(field_key):
                            sensitive_input = True
                        if not any(item["key"] == field_key for item in input_fields):
                            field_meta = {
                                "working_days": {
                                    "label": "Working days",
                                    "detail": "Example: Sunday, Monday, Tuesday, Wednesday, Thursday",
                                },
                                "opening_time": {
                                    "label": "Opening time",
                                    "type": "time",
                                },
                                "closing_time": {
                                    "label": "Closing time",
                                    "type": "time",
                                },
                                "slot_minutes": {
                                    "label": "Appointment duration (minutes)",
                                    "type": "number",
                                    "min": "5",
                                    "max": "720",
                                },
                                "capacity": {
                                    "label": "Bookings allowed per time slot",
                                    "type": "number",
                                    "min": "1",
                                    "max": "100",
                                },
                            }.get(field_key, {})
                            input_fields.append(
                                {
                                    "key": field_key,
                                    "label": str(
                                        input_labels.get(field_key)
                                        or field_meta.get("label")
                                        or raw_field
                                    ).strip()
                                    or field_key.replace("_", " "),
                                    "detail": str(
                                        input_purposes.get(field_key)
                                        or field_meta.get("detail")
                                        or ""
                                    ).strip()
                                    or None,
                                    **{
                                        meta_key: meta_value
                                        for meta_key, meta_value in field_meta.items()
                                        if meta_key not in {"label", "detail"}
                                    },
                                }
                            )
                    if sensitive_input:
                        waiting_reasons.append(
                            f"{key.replace('_', ' ').title()} must use a protected connection or credential setup path."
                        )
                    else:
                        setup_actions.append(
                            _builder_action(
                                "provide_input",
                                f"Provide {key.replace('_', ' ')}",
                                target="builder",
                                key=key,
                                detail=str(requirement.get("purpose") or "").strip() or None,
                                fields=input_fields,
                            )
                        )
            elif status == "connection_required":
                if kind == "channel" and key in missing_channels:
                    continue
                connection_status = str(
                    requirement.get("self_service_connection_status")
                    or self_service_connection_status(requirement)
                    or ""
                )
                if connection_status == "self_service_integration_available" and kind != "channel":
                    setup_actions.append(
                        _builder_action(
                            "connect_system",
                            f"Connect system for {key.replace('_', ' ')}",
                            target="integrations",
                            key=key,
                            detail=str(requirement.get("purpose") or "").strip() or None,
                        )
                    )
                elif connection_status == "xvond_adapter_required":
                    waiting_reasons.append(
                        f"{key.replace('_', ' ').title()} needs an Xvond connection adapter."
                    )
                elif connection_status == "xvond_managed_available":
                    waiting_reasons.append(
                        f"{key.replace('_', ' ').title()} will be provisioned by Xvond."
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

        graph_trigger = (
            (compiled_spec.get("delivery") or {}).get("graph_trigger")
            if isinstance(compiled_spec.get("delivery"), dict)
            else None
        )
        if (
            isinstance(graph_trigger, dict)
            and graph_trigger.get("trigger_type") == "webhook"
            and graph_trigger.get("status") == "ready"
            and graph_trigger.get("workflow_id")
        ):
            setup_actions.append(
                _builder_action(
                    "setup_webhook",
                    "Configure webhook trigger",
                    target="builder",
                    key="webhook_trigger",
                    detail="Copy the Xvond webhook URL and key into the external system that should trigger this employee.",
                )
            )

        if isinstance(graph_trigger, dict):
            graph_trigger_status = str(graph_trigger.get("status") or "not_required")
            if graph_trigger_status == "nested_approval_not_ready":
                waiting_reasons.append(
                    "This employee needs approval inside a foreach loop. Xvond must finish durable nested approval resume support before launch."
                )
            elif graph_trigger_status in {
                "schedule_required",
                "schedule_setup_required",
                "setup_required",
                "disabled",
            }:
                waiting_reasons.append(
                    "Xvond execution trigger setup is not ready yet."
                )

        for item in state.get("connected_system_setup") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("requirement_key") or "connected_system")
            setup_actions.append(
                _builder_action(
                    "manage_integrations",
                    "Validate connected system",
                    target="integrations",
                    key=key,
                    detail=str(item.get("message") or "").strip() or None,
                )
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

    compiled_at = str(builder.get("compiled_at") or "").strip()
    tested_build = bool(
        compiled_at
        and str(builder.get("last_tested_compiled_at") or "").strip() == compiled_at
    )
    setup_stage = next((item for item in stages if item.get("id") == "setup"), {})
    setup_complete = setup_stage.get("status") == "complete"
    if tested_build and setup_complete:
        add_stage(
            "test",
            "Preview & Test",
            "complete",
            "The current employee build has been tested safely without live channels or business actions.",
        )
    elif provisioned and has_entitlement and setup_complete:
        add_stage(
            "test",
            "Preview & Test",
            "action_required",
            "Chat with this exact draft before launch. Preview testing never sends through live channels or executes business actions.",
            [_builder_action("test_employee", "Test employee", target="builder")],
        )
    elif provisioned and not setup_complete:
        add_stage(
            "test",
            "Preview & Test",
            "blocked",
            "Finish the required setup for this build before preview testing.",
        )
    elif provisioned:
        add_stage(
            "test",
            "Preview & Test",
            "blocked",
            "Activate the AI Employee plan before testing this build.",
        )
    else:
        add_stage(
            "test",
            "Preview & Test",
            "blocked",
            "Build the employee before testing it.",
        )


    if agent.enabled:
        add_stage(
            "launch",
            "Launch",
            "complete",
            "This AI employee is live.",
        )
    elif state.get("ready") and tested_build:
        add_stage(
            "launch",
            "Launch",
            "action_required",
            "All launch requirements are ready and this build has been preview-tested.",
            [_builder_action("launch_employee", "Launch employee", target="builder")],
        )
    elif state.get("ready") and not tested_build:
        add_stage(
            "launch",
            "Launch",
            "blocked",
            "Test the current employee build once before launch.",
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
                builder=builder if isinstance(builder, dict) else {},
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
                "last_tested_at": builder.get("last_tested_at"),
                "versions": _builder_versions_view(builder),
                "current_build_tested": bool(
                    builder.get("compiled_at")
                    and builder.get("last_tested_compiled_at") == builder.get("compiled_at")
                ),
                "can_launch": bool(
                    self_service_state
                    and self_service_state.get("ready")
                    and builder.get("compiled_at")
                    and builder.get("last_tested_compiled_at") == builder.get("compiled_at")
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
                "setup_answers": {},
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

        if is_self_service:
            reconcile_managed_channel_requests(
                db,
                company_id=company.id,
                agent_id=agent.id,
                desired_channel_types=requested_channels,
                request_source="job_brief",
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
        builder = _snapshot_builder_version(
            builder,
            reason="job_brief_revision",
            capabilities=dict(config.capabilities or {}),
        )
        builder.update(
            {
                "version": 2,
                "source_description": blueprint.description,
                "job_brief": blueprint.description,
                "audience": blueprint.audience,
                "requested_channels": communication_channels(blueprint.channels),
                "permissions": dict(blueprint.permissions),
                "missing_information": list(blueprint.missing_information),
                "setup_answers": {},
                "onboarding_source": company.onboarding_source,
                "delivery_mode": "self_service",
                "compiled_spec": None,
            }
        )
        _clear_current_build_evidence(builder)
        settings["employee_builder"] = builder

        _clear_generated_self_service_build(
            db,
            company_id=company.id,
            agent_id=agent.id,
        )

        desired_channels = set(communication_channels(blueprint.channels))
        managed_reconcile = reconcile_managed_channel_requests(
            db,
            company_id=company.id,
            agent_id=agent.id,
            desired_channel_types=desired_channels,
            request_source="job_brief_revision",
        )
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
            "managed_channel_requests": managed_reconcile,
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


@router.post("/{agent_id}/refine")
def refine_self_service_employee(
    agent_id: int,
    data: EmployeeBuilderRefineRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Apply a concise owner instruction to the same draft employee.

    The source Job Brief remains auditable. Later OWNER REFINEMENT sections are
    compiled as authoritative overrides without silently discarding unrelated
    requirements.
    """
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Natural-language refinement is available only for Self-Service employees")
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
        config = _employee_config_or_404(db, agent)
        builder = dict((config.settings or {}).get("employee_builder") or {})
        current_brief = str(builder.get("source_description") or agent.description or "").strip()
        if not current_brief:
            raise HTTPException(409, "Current Job Brief is unavailable")
        instruction = " ".join(str(data.instruction or "").strip().split())
        if not instruction:
            raise HTTPException(400, "Refinement instruction is required")
        revised = (
            current_brief
            + "\n\nOWNER REFINEMENT "
            + datetime.utcnow().isoformat(timespec="seconds")
            + "Z:\n"
            + instruction
        )
        if len(revised) > 12000:
            raise HTTPException(
                409,
                "This employee has accumulated too many refinements. Consolidate the Job Brief before continuing.",
            )

        if agent.enabled:
            try:
                blueprint = _build_final_blueprint(
                    EmployeeBuilderCreateRequest(
                        description=revised,
                        name=agent.name,
                    )
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            pending = {
                "version": 1,
                "status": "draft",
                "source_description": blueprint.description,
                "job_brief": blueprint.description,
                "audience": blueprint.audience,
                "requested_channels": list(
                    communication_channels(blueprint.channels)
                ),
                "permissions": dict(blueprint.permissions),
                "capabilities": {
                    item: True for item in blueprint.capabilities
                },
                "setup_answers": deepcopy(builder.get("setup_answers") or {}),
                "base_compiled_at": builder.get("compiled_at"),
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "compiled_spec": None,
            }
            if _has_ai_agents_entitlement(db, company.id):
                staged = _compile_staged_employee_spec(
                    db,
                    company_id=company.id,
                    agent=agent,
                    job_brief=blueprint.description,
                    requested_channels=pending["requested_channels"],
                    previous_spec=(
                        builder.get("compiled_spec")
                        if isinstance(builder.get("compiled_spec"), dict)
                        else None
                    ),
                )
                pending.update(staged)
                pending["status"] = "built"

            settings_value = dict(config.settings or {})
            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": (
                    "revision_staged_and_built"
                    if pending.get("compiled_spec")
                    else "revision_staged"
                ),
                "agent_id": agent.id,
                "live_employee_unchanged": True,
                "pending_revision": {
                    "status": pending.get("status"),
                    "compiled": isinstance(pending.get("compiled_spec"), dict),
                    "compiled_at": pending.get("compiled_at"),
                    "created_at": pending.get("created_at"),
                    "job_brief": pending.get("source_description"),
                    "requested_channels": pending.get("requested_channels") or [],
                },
            }
    finally:
        db.close()

    result = revise_self_service_job_brief(
        agent_id,
        EmployeeBuilderReviseRequest(description=revised),
        current_user,
    )
    if _has_ai_agents_entitlement_for_user(current_user):
        try:
            compile_employee(agent_id, current_user)
            result["compiled"] = True
            result["status"] = "refined_and_rebuilt"
        except HTTPException:
            # The refinement itself is durable. The normal journey will surface
            # the build blocker rather than losing the owner's instruction.
            result["status"] = "refined"
    return result


def _has_ai_agents_entitlement_for_user(current_user: User) -> bool:
    db = SessionLocal()
    try:
        return _has_ai_agents_entitlement(db, current_user.company_id)
    finally:
        db.close()


@router.get("/{agent_id}/versions")
def self_service_employee_versions(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Version history is available only for Self-Service employees")
        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        builder = dict((config.settings or {}).get("employee_builder") or {})
        return {
            "agent_id": agent.id,
            "versions": _builder_versions_view(builder),
        }
    finally:
        db.close()


@router.post("/{agent_id}/rollback")
def rollback_self_service_employee(
    agent_id: int,
    data: EmployeeBuilderRollbackRequest,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Rollback is available only for Self-Service employees")
        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        if agent.enabled:
            raise HTTPException(409, "Deactivate this employee before rolling it back")

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        versions = list(builder.get("versions") or [])
        selected = next(
            (
                item for item in versions
                if isinstance(item, dict)
                and str(item.get("id") or "") == str(data.version_id)
            ),
            None,
        )
        if selected is None:
            raise HTTPException(404, "Employee version not found")

        previous_capabilities = dict(config.capabilities or {})
        builder = _snapshot_builder_version(
            builder,
            reason="before_rollback",
            capabilities=previous_capabilities,
        )
        restored_brief = str(selected.get("source_description") or "").strip()
        if not restored_brief:
            raise HTTPException(409, "Selected version has no Job Brief")

        _clear_generated_self_service_build(
            db,
            company_id=company.id,
            agent_id=agent.id,
        )

        restored_spec = selected.get("compiled_spec")
        restored_channels = communication_channels(selected.get("requested_channels") or [])
        restored_capabilities = dict(selected.get("capabilities") or {})
        builder["source_description"] = restored_brief
        builder["job_brief"] = restored_brief
        builder["requested_channels"] = restored_channels
        builder["audience"] = selected.get("audience")
        builder["permissions"] = deepcopy(dict(selected.get("permissions") or {}))
        builder["setup_answers"] = deepcopy(dict(selected.get("setup_answers") or {}))
        _clear_current_build_evidence(builder)

        if isinstance(restored_spec, dict):
            restored_spec = deepcopy(restored_spec)
            restored_spec, delivery = provision_compiled_capabilities(
                db,
                agent_id=agent.id,
                spec=restored_spec,
            )
            restored_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            builder["compiled_spec"] = restored_spec
            builder["delivery"] = delivery
            builder["compiled_at"] = restored_at
            builder["missing_information"] = list(restored_spec.get("setup_required") or [])
            agent.system_prompt = build_compiled_employee_system_prompt(
                owner_name=company.name,
                spec=restored_spec,
            )
        else:
            blueprint = _build_final_blueprint(
                EmployeeBuilderCreateRequest(
                    description=restored_brief,
                    name=agent.name,
                )
            )
            restored_capabilities = {item: True for item in blueprint.capabilities}
            builder["audience"] = blueprint.audience
            builder["permissions"] = dict(blueprint.permissions)
            builder["missing_information"] = list(blueprint.missing_information)
            agent.system_prompt = build_employee_system_prompt(
                owner_name=company.name,
                blueprint=blueprint,
            )

        config.capabilities = restored_capabilities
        _reconcile_builder_runtime_tools(
            db,
            agent_id=agent.id,
            previous_capabilities=previous_capabilities,
            next_capabilities=tuple(
                key
                for key, enabled in restored_capabilities.items()
                if enabled
            ),
        )
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        agent.description = restored_brief

        profile = (
            db.query(AIAgentProfile)
            .filter(
                AIAgentProfile.company_id == company.id,
                AIAgentProfile.agent_id == agent.id,
            )
            .first()
        )
        if profile is not None:
            profile.instructions = restored_brief
            if isinstance(restored_spec, dict):
                profile.business_type = (
                    "personal"
                    if str(restored_spec.get("scope") or "").strip().lower() == "personal"
                    else None
                )
            else:
                profile.business_type = (
                    "personal"
                    if str(builder.get("audience") or "").strip().lower() == "personal"
                    else None
                )

        reconcile_managed_channel_requests(
            db,
            company_id=company.id,
            agent_id=agent.id,
            desired_channel_types=restored_channels,
            request_source="version_rollback",
        )
        for channel in (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == company.id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.enabled.is_(True),
            )
            .with_for_update()
            .all()
        ):
            channel_type = canonical_channel_type(channel.channel_type)
            if communication_channels([channel_type]) and channel_type not in restored_channels:
                channel.enabled = False

        db.commit()
        return {
            "status": "rolled_back",
            "agent_id": agent.id,
            "version_id": data.version_id,
            "compiled": isinstance(builder.get("compiled_spec"), dict),
            "current_build_tested": False,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _relative_endpoint(value: str | None, *, required: bool = False) -> str | None:
    endpoint = str(value or "").strip()
    if not endpoint:
        if required:
            raise HTTPException(400, "Required integration endpoint is missing")
        return None
    if endpoint.startswith("//") or endpoint.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Integration operation endpoints must be relative paths")
    return "/" + endpoint.lstrip("/")


@router.post("/{agent_id}/connections/{requirement_key}")
def bind_self_service_integration(
    agent_id: int,
    requirement_key: str,
    data: EmployeeBuilderIntegrationBindRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Bind one customer-owned connected system to one compiled requirement."""

    key = normalize_requirement_key(requirement_key)
    if not key:
        raise HTTPException(400, "Connection requirement key is invalid")

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Connected-system binding is available only for Self-Service employees")

        agent = db.query(AIAgent).filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        if agent.enabled:
            raise HTTPException(409, "Deactivate this employee before changing connected systems")

        integration = db.query(CompanyIntegration).filter(
            CompanyIntegration.id == data.integration_id,
            CompanyIntegration.company_id == company.id,
            CompanyIntegration.enabled.is_(True),
        ).first()
        if integration is None:
            raise HTTPException(404, "Connected system not found or disabled")
        integration_config = reveal_config(integration.config) or {}
        if not integration_validation_ready(integration_config):
            raise HTTPException(
                409,
                "Validate this connected system successfully before binding it to the AI employee",
            )

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        compiled_spec = builder.get("compiled_spec")
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee before connecting a system")

        requirements = [
            dict(item) if isinstance(item, dict) else item
            for item in (compiled_spec.get("requirements") or [])
        ]
        requirement = next(
            (
                item for item in requirements
                if isinstance(item, dict)
                and normalize_requirement_key(item.get("key")) == key
            ),
            None,
        )
        if requirement is None:
            raise HTTPException(404, "Connection requirement not found in the current Job Brief")
        if str(requirement.get("status") or "").strip().lower() != "connection_required":
            raise HTTPException(409, "This requirement does not need an external connection")
        if str(requirement.get("kind") or "").strip().lower() == "channel":
            raise HTTPException(409, "Communication channels use their dedicated connection flow")

        executable_types = {"custom_api", "pos", "crm", "erp", "webhook", "instagram_publish"}
        required_connector_types = {
            "instagram_publish": {"instagram_publish"},
        }
        allowed_for_requirement = required_connector_types.get(key)
        if allowed_for_requirement and integration.integration_type not in allowed_for_requirement:
            raise HTTPException(
                409,
                f"{key.replace('_', ' ').title()} requires its packaged Xvond connector.",
            )
        if integration.integration_type not in executable_types:
            raise HTTPException(
                409,
                "This connected-system type does not have a generic execution adapter. Use Custom API or an Xvond packaged connector.",
            )
        if key == "booking" and integration.integration_type == "webhook":
            raise HTTPException(
                409,
                "Booking needs a two-way API so Xvond can verify availability before creating the booking",
            )

        execute_required = integration.integration_type in {
            "custom_api", "pos", "crm", "erp"
        }
        execute_endpoint = _relative_endpoint(
            data.execute_endpoint,
            required=execute_required,
        )
        availability_endpoint = _relative_endpoint(data.availability_endpoint)
        cancel_endpoint = _relative_endpoint(data.cancel_endpoint)

        if key == "booking" and integration.integration_type != "webhook":
            if not availability_endpoint:
                raise HTTPException(
                    400,
                    "Booking systems need an availability endpoint so the employee can check real slots",
                )
            if not execute_endpoint:
                raise HTTPException(
                    400,
                    "Booking systems need a booking/create endpoint",
                )

        operations = {}
        if execute_endpoint:
            operations["execute"] = {"method": "POST", "endpoint": execute_endpoint}
        if availability_endpoint:
            operations["availability"] = {
                "method": "POST",
                "endpoint": availability_endpoint,
            }
        if cancel_endpoint:
            operations["cancel"] = {"method": "POST", "endpoint": cancel_endpoint}

        requirement["integration_id"] = integration.id
        requirement["integration_type"] = integration.integration_type
        requirement["integration_operations"] = operations
        requirement["fulfillment_mode"] = "external_connection"
        requirement["validation_required"] = True
        requirement["requires_connection"] = True
        requirement["status"] = "xvond_build"
        requirement["delivery_mode"] = "compose"

        compiled_value = dict(compiled_spec)
        compiled_value["requirements"] = requirements
        compiled_value, delivery = provision_compiled_capabilities(
            db,
            agent_id=agent.id,
            spec=compiled_value,
        )
        builder["compiled_spec"] = compiled_value
        builder["delivery"] = delivery
        builder["missing_information"] = list(compiled_value.get("setup_required") or [])
        # Connection changes alter executable behavior and therefore invalidate
        # preview evidence for the previous build.
        builder.pop("last_tested_at", None)
        builder.pop("last_tested_compiled_at", None)
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        agent.system_prompt = build_compiled_employee_system_prompt(
            owner_name=company.name,
            spec=compiled_value,
        )

        db.commit()
        return {
            "status": "connected",
            "agent_id": agent.id,
            "requirement_key": key,
            "integration": {
                "id": integration.id,
                "name": integration.name,
                "type": integration.integration_type,
            },
            "compiled_spec": self_service_spec_view(compiled_value),
            "readiness": self_service_readiness(
                db,
                company=company,
                agent=agent,
                config=config,
            ),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.put("/{agent_id}/setup/{requirement_key}")
def save_self_service_setup_answer(
    agent_id: int,
    requirement_key: str,
    data: EmployeeBuilderSetupAnswerRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Save owner-provided setup data required by the compiled employee contract."""

    key = requirement_key.strip().lower()
    if not key or len(key) > 100:
        raise HTTPException(400, "Setup requirement key is invalid")
    if key in {"knowledge", "files"}:
        raise HTTPException(
            409,
            "Knowledge and files must be added through the employee Knowledge workspace",
        )
    if is_sensitive_requirement_key(key):
        raise HTTPException(
            409,
            "Sensitive credentials must use a protected Xvond connection path",
        )

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Setup answers are available only for Self-Service employees")

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

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        if agent.enabled:
            raise HTTPException(409, "Deactivate this employee before changing setup data")

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        compiled_spec = builder.get("compiled_spec")
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee before providing setup data")

        requirement = next(
            (
                item
                for item in (compiled_spec.get("requirements") or [])
                if isinstance(item, dict)
                and str(item.get("key") or "").strip().lower() == key
            ),
            None,
        )
        if requirement is None:
            raise HTTPException(404, "Setup requirement not found in the current Job Brief")
        if str(requirement.get("status") or "").strip().lower() != "customer_input_required":
            raise HTTPException(409, "This requirement does not accept customer setup data")

        declared_fields: list[str] = []
        for raw_field in requirement.get("customer_inputs") or []:
            field_key = normalize_requirement_key(raw_field)
            if not field_key or field_key in declared_fields:
                continue
            if is_sensitive_requirement_key(field_key):
                raise HTTPException(
                    409,
                    "Sensitive credentials must use a protected Xvond connection path",
                )
            declared_fields.append(field_key)

        if declared_fields:
            if len(data.values) > 30:
                raise HTTPException(400, "Too many setup fields")
            normalized_values = {
                normalize_requirement_key(raw_key): str(raw_value or "").strip()
                for raw_key, raw_value in data.values.items()
                if normalize_requirement_key(raw_key)
            }
            missing_fields = [
                field for field in declared_fields
                if not normalized_values.get(field)
            ]
            if missing_fields:
                raise HTTPException(
                    400,
                    detail={
                        "message": "Complete all required setup fields",
                        "missing_fields": missing_fields,
                    },
                )
            if any(len(normalized_values[field]) > 8000 for field in declared_fields):
                raise HTTPException(400, "Setup field is too long")
            answer_value: str | dict = {
                field: normalized_values[field] for field in declared_fields
            }
        else:
            value = str(data.value or "").strip()
            if not value:
                raise HTTPException(400, "Setup value is required")
            answer_value = value

        answers = dict(builder.get("setup_answers") or {})
        answers[key] = answer_value
        builder["setup_answers"] = answers

        compiled_value = dict(compiled_spec)
        customer_inputs = dict(compiled_value.get("customer_inputs") or {})
        customer_inputs[key] = answer_value
        compiled_value["customer_inputs"] = customer_inputs

        # Customer input is a build dependency, not a dead-end form. Once all
        # declared fields are supplied, advance the requirement back into its
        # declared build stage and re-provision the runtime capability.
        requirements = [
            dict(item) if isinstance(item, dict) else item
            for item in (compiled_value.get("requirements") or [])
        ]
        for item in requirements:
            if not isinstance(item, dict):
                continue
            if str(item.get("key") or "").strip().lower() != key:
                continue
            next_status = str(item.get("after_input_status") or "").strip().lower()
            if next_status:
                item["status"] = next_status
                item["delivery_mode"] = (
                    "compose" if next_status == "xvond_build" else item.get("delivery_mode")
                )
            break
        compiled_value["requirements"] = requirements

        compiled_value, delivery = provision_compiled_capabilities(
            db,
            agent_id=agent.id,
            spec=compiled_value,
        )
        builder["compiled_spec"] = compiled_value
        builder["delivery"] = delivery
        builder["missing_information"] = list(compiled_value.get("setup_required") or [])
        # Setup data changes the employee's executable behavior. A preview from
        # before this change cannot authorize launch of the updated build.
        builder.pop("last_tested_at", None)
        builder.pop("last_tested_compiled_at", None)
        settings_value["employee_builder"] = builder
        config.settings = settings_value

        agent.system_prompt = build_compiled_employee_system_prompt(
            owner_name=company.name,
            spec=compiled_value,
        )

        db.commit()
        return {
            "status": "saved",
            "agent_id": agent.id,
            "requirement_key": key,
            "compiled_spec": self_service_spec_view(compiled_value),
            "readiness": self_service_readiness(
                db,
                company=company,
                agent=agent,
                config=config,
            ),
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
        builder = dict((config.settings or {}).get("employee_builder") or {})
        compiled_at = str(builder.get("compiled_at") or "").strip()
        if not compiled_at or str(builder.get("last_tested_compiled_at") or "").strip() != compiled_at:
            raise HTTPException(
                409,
                detail={
                    "message": "Test the current employee build before launch",
                    "blockers": ["Run Preview & Test once after the latest build or revision"],
                },
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
        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        builder["last_tested_at"] = now_iso
        builder["last_tested_compiled_at"] = builder.get("compiled_at")
        builder["test_count"] = int(builder.get("test_count") or 0) + 1
        settings_value["employee_builder"] = builder
        config.settings = settings_value
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


@router.get("/{agent_id}/webhook")
def customer_employee_webhook(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Webhook setup is available for Self-Service employees")

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company_id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Employee not found")
        config = _employee_config_or_404(db, agent)
        builder = (config.settings or {}).get("employee_builder") or {}
        spec = builder.get("compiled_spec") if isinstance(builder, dict) else None
        delivery = spec.get("delivery") if isinstance(spec, dict) else None
        graph_trigger = delivery.get("graph_trigger") if isinstance(delivery, dict) else None
        if not isinstance(graph_trigger, dict) or graph_trigger.get("trigger_type") != "webhook":
            raise HTTPException(404, "This employee does not use a webhook trigger")
        if graph_trigger.get("status") != "ready":
            raise HTTPException(409, "Webhook trigger is not ready yet")

        workflow_id = int(graph_trigger.get("workflow_id") or 0)
        workflow = (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.id == workflow_id,
                AutomationWorkflow.company_id == company_id,
                AutomationWorkflow.trigger_type == "webhook",
                AutomationWorkflow.enabled.is_(True),
            )
            .first()
        )
        if workflow is None:
            raise HTTPException(409, "Webhook workflow is not active")
        if not settings.PUBLIC_BASE_URL:
            raise HTTPException(409, "PUBLIC_BASE_URL is not configured")

        return {
            "workflow_id": workflow.id,
            "url": f"{settings.PUBLIC_BASE_URL}/webhooks/automation/{workflow.id}",
            "header": "X-Xvond-Webhook-Key",
            "key": automation_webhook_key(
                workflow_id=workflow.id,
                company_id=company_id,
            ),
            "idempotency_header": "Idempotency-Key",
        }
    finally:
        db.close()


@router.get("/{agent_id}/automation-runs")
def customer_employee_automation_runs(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Automation runs are available for Self-Service employees")

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company_id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Employee not found")

        workflows = (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.company_id == company_id)
            .order_by(AutomationWorkflow.id.asc())
            .all()
        )
        workflow_ids = []
        workflow_names = {}
        for workflow in workflows:
            config = workflow.trigger_config if isinstance(workflow.trigger_config, dict) else {}
            if (
                config.get("_xvond_source") == "self_service_employee"
                and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
            ):
                workflow_ids.append(workflow.id)
                workflow_names[workflow.id] = workflow.name

        if not workflow_ids:
            return {"agent_id": agent.id, "runs": []}

        runs = (
            db.query(AutomationRun)
            .filter(
                AutomationRun.company_id == company_id,
                AutomationRun.workflow_id.in_(workflow_ids),
            )
            .order_by(AutomationRun.id.desc())
            .limit(50)
            .all()
        )
        return {
            "agent_id": agent.id,
            "runs": [
                {
                    "id": run.id,
                    "workflow_id": run.workflow_id,
                    "workflow_name": workflow_names.get(run.workflow_id),
                    "status": run.status,
                    "input_data": run.input_data,
                    "output_data": run.output_data,
                    "error_message": run.error_message,
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                }
                for run in runs
            ],
        }
    finally:
        db.close()


@router.post("/{agent_id}/run-graph")
def customer_employee_run_graph(
    agent_id: int,
    data: EmployeeBuilderGraphRunRequest,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not is_self_service_company(company):
            raise HTTPException(409, "Manual graph execution is available for Self-Service employees")

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company_id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Employee not found")
        if not agent.enabled:
            raise HTTPException(409, "Launch this employee before running its live execution graph")

        workflow = None
        for row in (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.company_id == company_id,
                AutomationWorkflow.trigger_type == "manual",
                AutomationWorkflow.enabled.is_(True),
            )
            .order_by(AutomationWorkflow.id.asc())
            .all()
        ):
            config = row.trigger_config if isinstance(row.trigger_config, dict) else {}
            if (
                config.get("_xvond_source") == "self_service_employee"
                and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
                and config.get("_xvond_graph_trigger") is True
            ):
                workflow = row
                break

        if workflow is None:
            raise HTTPException(409, "This employee does not have a ready manual execution graph")

        try:
            run = automation_runtime.execute(
                db=db,
                company_id=company_id,
                workflow=workflow,
                input_data=dict(data.input_data or {}),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        return {
            "id": run.id,
            "workflow_id": run.workflow_id,
            "status": run.status,
            "output_data": run.output_data,
            "error_message": run.error_message,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }
    finally:
        db.close()


def _automation_request_details(request: ActionRequest) -> dict:
    details = request.details if isinstance(request.details, dict) else {}
    return {
        key: value
        for key, value in details.items()
        if not str(key).startswith("_xvond_")
    }


@router.get("/{agent_id}/automation-approvals")
def customer_employee_automation_approvals(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        _company_or_404(db, company_id)
        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company_id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Employee not found")

        rows = (
            db.query(ActionRequest)
            .filter(
                ActionRequest.company_id == company_id,
                ActionRequest.agent_id == int(agent_id),
                ActionRequest.status == "awaiting_confirmation",
            )
            .order_by(ActionRequest.id.desc())
            .limit(100)
            .all()
        )
        approvals = []
        for request in rows:
            details = request.details if isinstance(request.details, dict) else {}
            meta = details.get("_xvond_automation")
            if not isinstance(meta, dict):
                continue
            approvals.append(
                {
                    "id": request.id,
                    "run_id": meta.get("run_id"),
                    "workflow_id": meta.get("workflow_id"),
                    "node_id": meta.get("node_id"),
                    "action_type": request.action_type,
                    "summary": request.summary,
                    "details": _automation_request_details(request),
                    "status": request.status,
                    "created_at": request.created_at,
                }
            )
        return {"agent_id": agent.id, "approvals": approvals}
    finally:
        db.close()


@router.post("/{agent_id}/automation-approvals/{request_id}/approve")
def customer_employee_approve_automation(
    agent_id: int,
    request_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        _company_or_404(db, company_id)

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company_id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "Employee not found")

        request = (
            db.query(ActionRequest)
            .filter(
                ActionRequest.id == int(request_id),
                ActionRequest.company_id == company_id,
                ActionRequest.agent_id == int(agent_id),
                ActionRequest.status == "awaiting_confirmation",
            )
            .with_for_update()
            .first()
        )
        if request is None:
            raise HTTPException(404, "Pending automation approval not found")
        details = request.details if isinstance(request.details, dict) else {}
        meta = details.get("_xvond_automation")
        if not isinstance(meta, dict):
            raise HTTPException(409, "Request is not an automation approval")

        run = (
            db.query(AutomationRun)
            .filter(
                AutomationRun.id == int(meta.get("run_id") or 0),
                AutomationRun.company_id == company_id,
                AutomationRun.status == "waiting_approval",
            )
            .with_for_update()
            .first()
        )
        workflow = (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.id == int(meta.get("workflow_id") or 0),
                AutomationWorkflow.company_id == company_id,
                AutomationWorkflow.enabled.is_(True),
            )
            .first()
        )
        if run is None or workflow is None:
            raise HTTPException(409, "Automation approval checkpoint is no longer runnable")

        request.status = "approved"
        db.flush()
        try:
            resumed = automation_runtime.resume_approval(
                db,
                company_id=company_id,
                workflow=workflow,
                run=run,
                request=request,
            )
        except Exception as exc:
            request = db.query(ActionRequest).filter(ActionRequest.id == int(request_id)).first()
            if request is not None:
                request.status = "approval_execution_failed"
                db.commit()
            raise HTTPException(409, str(exc)) from exc

        request = db.query(ActionRequest).filter(ActionRequest.id == int(request_id)).first()
        if request is not None:
            request.status = "confirmed"
            db.commit()

        return {
            "request_id": int(request_id),
            "run_id": resumed.id,
            "status": resumed.status,
            "output_data": resumed.output_data,
        }
    finally:
        db.close()


@router.post("/{agent_id}/automation-approvals/{request_id}/reject")
def customer_employee_reject_automation(
    agent_id: int,
    request_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        _company_or_404(db, company_id)

        request = (
            db.query(ActionRequest)
            .filter(
                ActionRequest.id == int(request_id),
                ActionRequest.company_id == company_id,
                ActionRequest.agent_id == int(agent_id),
                ActionRequest.status == "awaiting_confirmation",
            )
            .with_for_update()
            .first()
        )
        if request is None:
            raise HTTPException(404, "Pending automation approval not found")
        details = request.details if isinstance(request.details, dict) else {}
        meta = details.get("_xvond_automation")
        if not isinstance(meta, dict):
            raise HTTPException(409, "Request is not an automation approval")

        run = (
            db.query(AutomationRun)
            .filter(
                AutomationRun.id == int(meta.get("run_id") or 0),
                AutomationRun.company_id == company_id,
                AutomationRun.status == "waiting_approval",
            )
            .with_for_update()
            .first()
        )
        request.status = "rejected"
        if run is not None:
            output = dict(run.output_data or {})
            approval = output.get("approval")
            if isinstance(approval, dict):
                output["approval"] = {**approval, "status": "rejected"}
            run.status = "rejected"
            run.finished_at = datetime.utcnow()
            trace = output.get("trace")
            if isinstance(trace, dict):
                trace = dict(trace)
                trace["status"] = "rejected"
                trace["finished_at"] = (
                    run.finished_at.isoformat(timespec="milliseconds") + "Z"
                )
                output["trace"] = trace
            run.output_data = output
            run.error_message = None
        db.commit()
        return {
            "request_id": request.id,
            "run_id": run.id if run is not None else None,
            "status": "rejected",
        }
    finally:
        db.close()
