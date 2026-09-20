from copy import deepcopy
import hashlib
import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from backend.app.core.ai.engine import ProviderExecutionError, ai_engine
from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.company_lifecycle import portal_access_allowed
from backend.app.core.config_secrets import reveal_config
from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import (
    require_customer_admin,
    require_customer_manager,
)
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
    is_self_service_employee,
    self_service_channel_activation_blockers,
    self_service_channel_slots,
    self_service_connection_status,
    self_service_readiness,
    self_service_spec_view,
)
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.execution_graph import (
    graph_contract_errors,
    normalize_execution_graph,
)
from backend.app.modules.automation.runtime import automation_runtime
from backend.app.modules.automation.schedule import (
    ScheduleConfigError,
    next_schedule_slot,
)
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
from backend.app.modules.integrations.json_contract import sanitize_json_contract
from backend.app.modules.files.models import EmployeeFileAsset
from backend.app.modules.integrations.catalog import (
    compatible_integration_types,
    get_integration_definition,
    executable_integration_types,
    integration_packaged_operations,
    integration_requires_operation_endpoints,
    integration_validation_ready,
    validate_integration_config,
)
from backend.app.modules.integrations.capability_discovery import (
    api_connection_probe,
    discover_openapi_contract,
    oauth_client_credentials_token,
    public_api_probe,
)
from backend.app.modules.integrations.oauth_authorization import (
    consume_oauth_state,
    create_oauth_authorization,
    exchange_authorization_code,
    oauth_token_timing,
)

router = APIRouter(
    prefix="/customer/employee-builder",
    tags=["Customer Employee Builder"],
)

SELF_SERVICE_FREE_TEST_MESSAGES = 0
MAX_EMPLOYEE_FILE_BYTES = 15 * 1024 * 1024


def _self_service_commercial_gating() -> bool:
    """Billing is deliberately disabled while the Replit-style experiment is free."""
    return bool(
        settings.SELF_SERVICE_REQUIRE_SUBSCRIPTION
        and not settings.SELF_SERVICE_FREE_EXPERIMENT
    )


def _safe_asset_filename(value: str | None) -> str:
    filename = re.split(r"[\\/]", str(value or "file").strip())[-1].strip()
    filename = re.sub(r"[\x00-\x1f\x7f]+", "_", filename)
    return (filename or "file")[:255]




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
    operations: dict[str, dict] = Field(default_factory=dict)
    operation_map: dict[str, str] = Field(default_factory=dict)


class EmployeeBuilderDiscoveryAccessRequest(BaseModel):
    api_key: str | None = Field(default=None, min_length=1, max_length=8000)
    username: str | None = Field(default=None, min_length=1, max_length=500)
    password: str | None = Field(default=None, min_length=1, max_length=8000)
    client_id: str | None = Field(default=None, min_length=1, max_length=1000)
    client_secret: str | None = Field(default=None, min_length=1, max_length=8000)


class EmployeeBuilderSetupAnswerRequest(BaseModel):
    value: str | None = Field(default=None, max_length=8000)
    values: dict[str, str] = Field(default_factory=dict)

class EmployeeBuilderGraphRunRequest(BaseModel):
    input_data: dict = Field(default_factory=dict)
    routine_id: str | None = Field(default=None, max_length=80)


class EmployeeBuilderPermissionRequest(BaseModel):
    mode: str = Field(min_length=4, max_length=20)


class EmployeeBuilderRoutineStateRequest(BaseModel):
    enabled: bool


class EmployeeBuilderRoutineRetryRequest(BaseModel):
    run_id: int = Field(gt=0)


class EmployeeBuilderRoutinePreviewRequest(BaseModel):
    routine_id: str = Field(min_length=1, max_length=80)
    input_data: dict = Field(default_factory=dict)
    simulated_outputs: dict = Field(default_factory=dict)
    event_payloads: dict = Field(default_factory=dict)


DEFAULT_CUSTOMER_CONTROLS = {
    "can_enable_disable": True,
    "can_view_conversations": True,
    "can_view_usage": True,
    "can_edit_prompt": True,
    "can_change_provider": False,
    "can_change_model": False,
}


BUILDER_HISTORY_LIMIT = 20
OWNER_PERMISSION_MODES = {"automatic", "ask_before", "never"}


def _compiled_execution_routines(spec: dict | None) -> list[dict]:
    value = spec if isinstance(spec, dict) else {}
    result: list[dict] = []
    used: set[str] = set()

    raw_routines = value.get("execution_routines")
    if isinstance(raw_routines, list):
        for index, raw in enumerate(raw_routines):
            if not isinstance(raw, dict):
                continue
            graph = normalize_execution_graph(
                raw.get("graph")
                if isinstance(raw.get("graph"), dict)
                else raw.get("execution_graph")
            )
            if not graph.get("nodes"):
                continue
            routine_id = (
                normalize_requirement_key(
                    raw.get("id")
                    or raw.get("key")
                    or raw.get("name")
                    or f"routine_{index + 1}"
                )
                or f"routine_{index + 1}"
            )
            if routine_id in used:
                continue
            used.add(routine_id)
            result.append(
                {
                    "id": routine_id,
                    "name": str(
                        raw.get("name")
                        or routine_id.replace("_", " ").title()
                    )[:200],
                    "requirement_keys": [
                        key
                        for key in (
                            normalize_requirement_key(item)
                            for item in (raw.get("requirement_keys") or [])
                        )
                        if key
                    ],
                    "requirement_scope_declared": "requirement_keys" in raw,
                    "graph": graph,
                }
            )

    if result:
        return result

    graph = normalize_execution_graph(value.get("execution_graph") or {})
    if graph.get("nodes"):
        return [
            {
                "id": "primary",
                "name": "Primary routine",
                "requirement_keys": [],
                "requirement_scope_declared": False,
                "graph": graph,
            }
        ]
    return []


def _routine_preview_defaults(spec: dict, routine: dict) -> dict:
    requirements = [
        item
        for item in (spec.get("requirements") or [])
        if isinstance(item, dict)
    ]
    requirements_by_key = {
        normalize_requirement_key(item.get("key")): item
        for item in requirements
        if normalize_requirement_key(item.get("key"))
    }
    keys = [
        key
        for key in (
            normalize_requirement_key(item)
            for item in (routine.get("requirement_keys") or [])
        )
        if key and key in requirements_by_key
    ]

    selected = (
        [requirements_by_key[key] for key in keys]
        if routine.get("requirement_scope_declared") is True
        else requirements
    )
    defaults: dict = {}
    conflicts: list[str] = []
    for requirement in selected:
        for raw_key, value in (requirement.get("runtime_inputs") or {}).items():
            key = str(raw_key)
            if key in defaults and defaults[key] != value:
                if key not in conflicts:
                    conflicts.append(key)
                continue
            defaults.setdefault(key, deepcopy(value))

    if conflicts:
        raise HTTPException(
            409,
            detail={
                "message": "Routine runtime inputs are ambiguous",
                "conflicting_keys": conflicts,
            },
        )
    return defaults


def _invalidate_preview_evidence(container: dict) -> None:
    """Invalidate preview evidence after executable build or setup changes."""

    for key in (
        "last_tested_at",
        "last_tested_compiled_at",
        "chat_tested_at",
        "chat_tested_compiled_at",
        "routine_preview_evidence",
    ):
        container.pop(key, None)


def _routine_preview_evidence(
    container: dict,
    *,
    spec: dict,
    routine_id: str | None = None,
    record: bool = False,
) -> tuple[list[str], list[str], bool]:
    routines = _compiled_execution_routines(spec)
    required = [str(item.get("id") or "") for item in routines if item.get("id")]
    compiled_at = str(container.get("compiled_at") or "").strip()
    evidence = (
        deepcopy(container.get("routine_preview_evidence"))
        if isinstance(container.get("routine_preview_evidence"), dict)
        else {}
    )

    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if record and routine_id:
        evidence[str(routine_id)] = {
            "compiled_at": compiled_at,
            "tested_at": now_iso,
        }
        container["routine_preview_evidence"] = evidence
        container["routine_test_count"] = int(container.get("routine_test_count") or 0) + 1

    tested = [
        item
        for item in required
        if isinstance(evidence.get(item), dict)
        and str(evidence[item].get("compiled_at") or "") == compiled_at
    ]
    complete = bool(required) and len(tested) == len(required)
    if record and complete:
        container["last_tested_at"] = now_iso
        container["last_tested_compiled_at"] = compiled_at
        if "status" in container:
            container["status"] = "tested"
    return required, tested, complete


def _effective_compiled_permissions(spec: dict, builder: dict) -> dict:
    """Resolve compiler suggestions against explicit company-owner grants.

    Compiler output may suggest automatic execution, but it cannot grant that
    authority. Owner/admin overrides are keyed by normalized requirement key.
    """

    prepared = deepcopy(spec)
    raw_owner = builder.get("owner_permissions")
    owner_permissions = {
        normalize_requirement_key(key): str(value or "").strip().lower()
        for key, value in (raw_owner.items() if isinstance(raw_owner, dict) else [])
        if normalize_requirement_key(key)
        and str(value or "").strip().lower() in OWNER_PERMISSION_MODES
    }

    raw_permissions = [
        dict(item)
        for item in (prepared.get("permissions") or [])
        if isinstance(item, dict) and str(item.get("action") or "").strip()
    ]
    consumed: set[int] = set()
    effective: list[dict] = []

    for requirement in prepared.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        key = normalize_requirement_key(requirement.get("key"))
        if not key:
            continue
        targets = {
            key,
            normalize_requirement_key(requirement.get("purpose")),
        } - {""}
        matching: list[tuple[int, dict]] = []
        for index, permission in enumerate(raw_permissions):
            action_key = normalize_requirement_key(permission.get("action"))
            if action_key in targets:
                matching.append((index, permission))
                consumed.add(index)

        suggested_modes = [
            str(
                permission.get("suggested_mode")
                or permission.get("mode")
                or "ask_before"
            ).strip().lower()
            for _, permission in matching
        ]
        suggested_mode = None
        if "never" in suggested_modes:
            suggested_mode = "never"
        elif "automatic" in suggested_modes:
            suggested_mode = "automatic"
        elif matching:
            suggested_mode = "ask_before"

        owner_mode = owner_permissions.get(key)
        if owner_mode is not None:
            effective.append(
                {
                    "action": key,
                    "mode": owner_mode,
                    "suggested_mode": suggested_mode or "ask_before",
                    "source": "owner",
                }
            )
        elif matching:
            effective.append(
                {
                    "action": key,
                    "mode": "never" if suggested_mode == "never" else "ask_before",
                    "suggested_mode": suggested_mode or "ask_before",
                    "source": "compiler_suggestion",
                }
            )

    for index, permission in enumerate(raw_permissions):
        if index in consumed:
            continue
        suggested_mode = str(
            permission.get("suggested_mode")
            or permission.get("mode")
            or "ask_before"
        ).strip().lower()
        if suggested_mode not in OWNER_PERMISSION_MODES:
            suggested_mode = "ask_before"
        effective.append(
            {
                "action": str(permission.get("action") or "").strip()[:500],
                "mode": "never" if suggested_mode == "never" else "ask_before",
                "suggested_mode": suggested_mode,
                "source": "compiler_suggestion",
            }
        )
        if len(effective) >= 50:
            break

    prepared["permissions"] = effective[:50]
    prepared["owner_permissions"] = owner_permissions
    return prepared


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
            "owner_permissions": deepcopy(dict(builder.get("owner_permissions") or {})),
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
        .order_by(AIAgent.id.desc())
        .first()
    )


def _employee_for_company(db, company_id: int, agent_id: int | None = None) -> AIAgent | None:
    query = (
        db.query(AIAgent)
        .join(AgentConfig, AgentConfig.agent_id == AIAgent.id)
        .filter(
            AIAgent.company_id == company_id,
            AgentConfig.agent_type == "employee",
        )
    )
    if agent_id is not None:
        query = query.filter(AIAgent.id == agent_id)
    return query.order_by(AIAgent.id.desc()).first()


def _employee_for_workspace(
    db,
    company_id: int,
    agent_id: int | None = None,
) -> AIAgent | None:
    if agent_id is None:
        return _existing_employee(db, company_id)
    return (
        db.query(AIAgent)
        .join(AgentConfig, AgentConfig.agent_id == AIAgent.id)
        .filter(
            AIAgent.company_id == company_id,
            AIAgent.id == agent_id,
            AgentConfig.agent_type == "employee",
        )
        .first()
    )


def _self_service_employee_or_404(
    db,
    *,
    company: Company,
    agent_id: int,
) -> tuple[AIAgent, AgentConfig]:
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == int(agent_id),
            AIAgent.company_id == company.id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(404, "AI employee not found")
    config = _employee_config_or_404(db, agent)
    if not is_self_service_employee(company, config):
        raise HTTPException(
            409,
            "Managed employees must use the Xvond Managed delivery flow",
        )
    return agent, config


def _agent_id_is_self_service(
    db,
    company: Company,
    agent_id: int,
) -> bool:
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == int(agent_id),
            AIAgent.company_id == company.id,
        )
        .first()
    )
    if agent is None:
        return False
    config = (
        db.query(AgentConfig)
        .filter(
            AgentConfig.agent_id == agent.id,
            AgentConfig.agent_type == "employee",
        )
        .first()
    )
    # Legacy Self-Service records created before employee_builder delivery_mode
    # existed inherit the company source. New mixed-delivery accounts always
    # carry an explicit per-agent delivery_mode.
    if config is None:
        return is_self_service_company(company)
    return is_self_service_employee(company, config)


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


def _compiled_discovery_summary(spec: dict | None) -> dict:
    pending: list[dict] = []
    for item in ((spec or {}).get("requirements") or []):
        if not isinstance(item, dict):
            continue
        discovery = item.get("discovery")
        if not isinstance(discovery, dict) or discovery.get("needed") is not True:
            continue
        pending.append(
            {
                "requirement_key": normalize_requirement_key(item.get("key")),
                "capability": str(discovery.get("capability") or "")[:500],
                "service_hint": str(discovery.get("service_hint") or "")[:160],
                "docs_url": str(discovery.get("docs_url") or "")[:1200],
                "search_queries": list(discovery.get("search_queries") or [])[:5],
                "customer_access": str(discovery.get("customer_access") or "unknown"),
                "status": str(discovery.get("status") or "pending_discovery"),
            }
        )
    return {
        "needed": bool(pending),
        "pending_count": len(pending),
        "requirements": pending,
    }


def _attempt_compiled_capability_discovery(
    db,
    *,
    company: Company,
    agent: AIAgent,
    spec: dict,
) -> tuple[dict, list[dict]]:
    """Resolve pending discovery plans during Build without user intervention."""
    updated = deepcopy(spec)
    requirements = [
        dict(item) if isinstance(item, dict) else item
        for item in (updated.get("requirements") or [])
    ]
    outcomes: list[dict] = []

    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        key = normalize_requirement_key(requirement.get("key"))
        discovery = requirement.get("discovery")
        if not key or not isinstance(discovery, dict) or discovery.get("needed") is not True:
            continue
        if str(discovery.get("status") or "pending_discovery") == "resolved":
            continue

        result = discover_openapi_contract(discovery)
        if result.get("status") != "resolved" or not isinstance(result.get("contract"), dict):
            discovery = dict(discovery)
            discovery["status"] = "not_found"
            discovery["attempted"] = list(result.get("attempted") or [])[:20]
            requirement["discovery"] = discovery
            outcomes.append({"requirement_key": key, "status": "not_found"})
            continue

        contract = dict(result["contract"])
        operations = _bounded_connection_operations(contract.get("operations") or {})
        if not operations:
            outcomes.append({"requirement_key": key, "status": "no_operations"})
            continue

        discovery = dict(discovery)
        discovery.update({
            "status": "contract_found",
            "source": result.get("source"),
            "docs_url": result.get("docs_url"),
            "contract_title": str(contract.get("title") or "")[:200],
            "base_url": str(contract.get("base_url") or "")[:1200],
            "auth_schemes": list(contract.get("auth_schemes") or [])[:10],
            "operation_count": len(operations),
            "attempted": list(result.get("attempted") or [])[:20],
        })
        requirement["discovery"] = discovery
        requirement["integration_operations"] = operations
        requirement["requires_connection"] = True
        requirement["fulfillment_mode"] = "external_connection"
        requirement["status"] = "connection_required"
        requirement["delivery_mode"] = "connect_and_compose"

        auto_provisioned = False
        access_mode = str(discovery.get("customer_access") or "unknown").strip().lower()
        if access_mode == "none":
            evidence = public_api_probe(contract)
            base_url = str(contract.get("base_url") or "").strip().rstrip("/")
            if evidence and base_url:
                integration_config = {
                    "base_url": base_url,
                    "validation_endpoint": str(evidence.get("endpoint") or ""),
                    "auth_type": "none",
                    "operations": operations,
                    "_xvond_validation": evidence,
                    "_xvond_discovery": {
                        "source": result.get("source"),
                        "docs_url": result.get("docs_url"),
                        "requirement_key": key,
                    },
                }
                validate_integration_config("custom_api", integration_config)
                current = (
                    db.query(CompanyIntegration)
                    .filter(
                        CompanyIntegration.company_id == company.id,
                        CompanyIntegration.enabled.is_(True),
                    )
                    .count()
                )
                service_limits.check_current(
                    db, company.id, "ai_agents", "integrations", current
                )
                integration = CompanyIntegration(
                    company_id=company.id,
                    integration_type="custom_api",
                    name=(
                        str(discovery.get("service_hint") or "").strip()
                        or str(contract.get("title") or "").strip()
                        or key.replace("_", " ").title()
                    )[:200],
                    config=integration_config,
                    enabled=True,
                )
                db.add(integration)
                db.flush()
                requirement["integration_id"] = integration.id
                requirement["integration_type"] = "custom_api"
                requirement["validation_required"] = True
                requirement["status"] = "xvond_build"
                requirement["delivery_mode"] = "compose"
                discovery["status"] = "resolved"
                discovery["auto_provisioned"] = True
                auto_provisioned = True

        outcomes.append({
            "requirement_key": key,
            "status": "resolved" if auto_provisioned else "contract_found",
            "customer_access": access_mode,
            "operation_count": len(operations),
            "_integration_id": requirement.get("integration_id") if auto_provisioned else None,
        })

    updated["requirements"] = requirements
    for outcome in outcomes:
        if outcome.get("status") == "resolved":
            resolved_key = normalize_requirement_key(outcome.get("requirement_key"))
            updated["setup_required"] = [
                item
                for item in (updated.get("setup_required") or [])
                if normalize_requirement_key(item) != resolved_key
            ]
            matching = next(
                (
                    item for item in requirements
                    if isinstance(item, dict)
                    and normalize_requirement_key(item.get("key")) == resolved_key
                ),
                None,
            )
            if isinstance(matching, dict):
                updated, unresolved = _resolve_bound_graph_operations(
                    updated,
                    requirement_key=resolved_key,
                    operations=matching.get("integration_operations") or {},
                )
                if unresolved:
                    matching = next(
                        item for item in updated["requirements"]
                        if isinstance(item, dict)
                        and normalize_requirement_key(item.get("key")) == resolved_key
                    )
                    matching["status"] = "connection_required"
                    matching["delivery_mode"] = "connect_and_compose"
                    matching.pop("integration_id", None)
                    matching.pop("integration_type", None)
                    matching["discovery"]["status"] = "operation_selection_required"
                    orphan_id = outcome.get("_integration_id")
                    if orphan_id:
                        orphan = (
                            db.query(CompanyIntegration)
                            .filter(
                                CompanyIntegration.id == int(orphan_id),
                                CompanyIntegration.company_id == company.id,
                            )
                            .first()
                        )
                        if orphan is not None:
                            db.delete(orphan)
                    outcome["status"] = "operation_selection_required"

    for outcome in outcomes:
        outcome.pop("_integration_id", None)
    return updated, outcomes


def _compiler_connection_context(db, *, company_id: int) -> list[dict]:
    """Expose validated connection capabilities to the compiler without secrets or IDs."""
    rows = (
        db.query(CompanyIntegration)
        .filter(
            CompanyIntegration.company_id == company_id,
            CompanyIntegration.enabled.is_(True),
        )
        .order_by(CompanyIntegration.id.asc())
        .limit(20)
        .all()
    )
    result: list[dict] = []
    executable = executable_integration_types()
    for item in rows:
        integration_type = str(item.integration_type or "").strip().lower()
        definition = get_integration_definition(integration_type) or {}
        if integration_type not in executable:
            continue
        plain = reveal_config(item.config) or {}
        if not integration_validation_ready(plain):
            continue
        operations = integration_packaged_operations(integration_type)
        raw_operations = plain.get("operations")
        if isinstance(raw_operations, dict):
            try:
                operations.update(_bounded_connection_operations(raw_operations))
            except HTTPException:
                operations = {}
        result.append(
            {
                "name": str(item.name or "")[:120],
                "type": integration_type,
                "capabilities": [
                    normalize_requirement_key(value)
                    for value in (definition.get("requirement_keys") or [])
                    if normalize_requirement_key(value)
                ][:20],
                "operations": {
                    key: {
                        "method": str(value.get("method") or "").upper(),
                        "endpoint": str(value.get("endpoint") or "")[:500],
                        "input_mode": str(value.get("input_mode") or "")[:20],
                        "path_params": list(value.get("path_params") or [])[:20],
                        "header_params": list(value.get("header_params") or [])[:50],
                        "required_header_params": list(value.get("required_header_params") or [])[:50],
                        "query_params": list(value.get("query_params") or [])[:50],
                        "required_query_params": list(value.get("required_query_params") or [])[:50],
                        "required_json_fields": list(value.get("required_json_fields") or [])[:50],
                        "json_fields": [
                            dict(field)
                            for field in (value.get("json_fields") or [])[:50]
                            if isinstance(field, dict)
                        ],
                        "required_form_fields": list(value.get("required_form_fields") or [])[:50],
                        "form_fields": [
                            dict(field)
                            for field in (value.get("form_fields") or [])[:50]
                            if isinstance(field, dict)
                        ],
                        "array_item_kind": str(value.get("array_item_kind") or "")[:20],
                        "array_max_items": int(value.get("array_max_items") or 100),
                        "required_array_item_fields": list(
                            value.get("required_array_item_fields") or []
                        )[:50],
                        "array_item_fields": [
                            dict(field)
                            for field in (value.get("array_item_fields") or [])[:50]
                            if isinstance(field, dict)
                        ],
                        "response_status": str(value.get("response_status") or "")[:3],
                        "response_kind": str(value.get("response_kind") or "")[:20],
                        "response_fields": [
                            dict(field)
                            for field in (value.get("response_fields") or [])[:25]
                            if isinstance(field, dict)
                        ],
                        "response_item_kind": str(value.get("response_item_kind") or "")[:20],
                        "response_item_fields": [
                            dict(field)
                            for field in (value.get("response_item_fields") or [])[:25]
                            if isinstance(field, dict)
                        ],
                        "description": str(value.get("description") or "")[:300],
                    }
                    for key, value in list(operations.items())[:30]
                    if isinstance(value, dict)
                },
            }
        )
        if len(result) >= 12:
            break
    return result


def _auto_bind_single_packaged_integrations(
    db,
    *,
    company_id: int,
    spec: dict,
) -> tuple[dict, list[str]]:
    """Bind one unambiguous validated connector without asking twice.

    Packaged connectors bind by declared capability. Generic HTTP APIs may bind
    only when the compiler reused an exact operation contract that exists on one
    validated connection. Multiple matches remain owner-controlled.
    """

    updated = deepcopy(spec)
    requirements = [
        dict(item) if isinstance(item, dict) else item
        for item in (updated.get("requirements") or [])
    ]
    integrations = (
        db.query(CompanyIntegration)
        .filter(
            CompanyIntegration.company_id == company_id,
            CompanyIntegration.enabled.is_(True),
        )
        .order_by(CompanyIntegration.id.asc())
        .all()
    )
    executable = executable_integration_types()
    bound_keys: list[str] = []

    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        if str(requirement.get("status") or "").strip().lower() != "connection_required":
            continue
        if str(requirement.get("kind") or "").strip().lower() == "channel":
            continue
        if requirement.get("integration_id"):
            continue

        key = normalize_requirement_key(requirement.get("key"))
        if not key:
            continue
        compatible = compatible_integration_types(key)
        try:
            required_operations = _bounded_connection_operations(
                requirement.get("integration_operations")
                if isinstance(requirement.get("integration_operations"), dict)
                else {}
            )
        except HTTPException:
            required_operations = {}
        candidates: list[tuple[CompanyIntegration, dict]] = []

        for integration in integrations:
            integration_type = str(integration.integration_type or "").strip().lower()
            definition = get_integration_definition(integration_type) or {}
            if integration_type not in executable or integration_type not in compatible:
                continue
            plain_config = reveal_config(integration.config) or {}
            if not integration_validation_ready(plain_config):
                continue

            packaged_keys = {
                normalize_requirement_key(item)
                for item in (definition.get("requirement_keys") or [])
                if normalize_requirement_key(item)
            }
            if (
                key in packaged_keys
                and not integration_requires_operation_endpoints(integration_type)
            ):
                candidates.append(
                    (integration, integration_packaged_operations(integration_type))
                )
                continue

            if not required_operations or definition.get("generic_requirements") is not True:
                continue
            try:
                configured_operations = _bounded_connection_operations(
                    plain_config.get("operations")
                    if isinstance(plain_config.get("operations"), dict)
                    else {}
                )
            except HTTPException:
                continue
            matched: dict[str, dict] = {}
            for operation_key, required in required_operations.items():
                configured = configured_operations.get(operation_key)
                if not isinstance(configured, dict):
                    matched = {}
                    break
                if (
                    str(configured.get("method") or "").upper()
                    != str(required.get("method") or "").upper()
                    or str(configured.get("endpoint") or "")
                    != str(required.get("endpoint") or "")
                ):
                    matched = {}
                    break
                matched[operation_key] = dict(configured)
            if matched and len(matched) == len(required_operations):
                candidates.append((integration, matched))

        if len(candidates) != 1:
            continue

        integration, matched_operations = candidates[0]
        integration_type = str(integration.integration_type or "").strip().lower()
        requirement["integration_id"] = integration.id
        requirement["integration_type"] = integration_type
        requirement["integration_operations"] = matched_operations
        requirement["fulfillment_mode"] = "external_connection"
        requirement["validation_required"] = True
        requirement["requires_connection"] = True
        requirement["status"] = "xvond_build"
        requirement["delivery_mode"] = "compose"
        bound_keys.append(key)

    updated["requirements"] = requirements
    if bound_keys:
        bound = set(bound_keys)
        updated["setup_required"] = [
            item
            for item in (updated.get("setup_required") or [])
            if normalize_requirement_key(item) not in bound
        ]
    return updated, bound_keys


def _store_provisioned_spec(
    db, *, company_id: int, agent: AIAgent, config: AgentConfig,
    settings: dict, builder: dict, spec: dict,
) -> dict:
    spec, auto_bound = _auto_bind_single_packaged_integrations(
        db,
        company_id=company_id,
        spec=spec,
    )
    if auto_bound:
        # Executable behavior changed, so evidence from a prior preview cannot
        # authorize launch of the newly connected build.
        _invalidate_preview_evidence(builder)
    spec = _effective_compiled_permissions(spec, builder)
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
    if is_self_service_employee(company, config):
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
    compiled_role = str(compiled_spec.get("role") or "").strip()
    if compiled_role and str(agent.name or "").strip() in {"", "My AI Employee", "AI Employee"}:
        agent.name = compiled_role[:200]
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
    if is_self_service_employee(company, config):
        requested_channels = communication_channels(requested_channels)
    connection_context = _compiler_connection_context(db, company_id=company_id)

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
                    available_connections=connection_context,
                ),
                model=candidate.model,
                tools=None,
            )
            candidate_spec = parse_compiler_response(
                candidate_response.text,
                job_brief=job_brief,
            )
            candidate_spec, discovery_outcomes = _attempt_compiled_capability_discovery(
                db,
                company=company,
                agent=agent,
                spec=candidate_spec,
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
    previous_inputs = (
        dict((previous_spec or {}).get("customer_inputs") or {})
        if isinstance(previous_spec, dict)
        else {}
    )
    carried_inputs: dict = {}
    for raw in updated.get("requirements") or []:
        if not isinstance(raw, dict):
            requirements.append(raw)
            continue
        item = dict(raw)
        key = normalize_requirement_key(item.get("key"))
        prior = previous.get(key)
        if isinstance(prior, dict):
            for field in carry_fields:
                if field in prior and field not in item:
                    item[field] = deepcopy(prior[field])
            if (
                prior.get("integration_id")
                and str(item.get("status") or "").strip().lower()
                == "connection_required"
            ):
                item["status"] = "xvond_build"
                item["delivery_mode"] = "compose"
        if key and key in previous_inputs:
            carried_inputs[key] = deepcopy(previous_inputs[key])
            if (
                str(item.get("status") or "").strip().lower()
                == "customer_input_required"
            ):
                next_status = str(
                    item.get("after_input_status") or "xvond_build"
                ).strip().lower()
                item["status"] = next_status
                if next_status == "xvond_build":
                    item["delivery_mode"] = "compose"
        requirements.append(item)
    updated["requirements"] = requirements
    if carried_inputs:
        updated["customer_inputs"] = {
            **dict(updated.get("customer_inputs") or {}),
            **carried_inputs,
        }
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
    connection_context = _compiler_connection_context(db, company_id=company_id)
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
                    available_connections=connection_context,
                ),
                model=candidate.model,
                tools=None,
            )
            spec = parse_compiler_response(response.text, job_brief=job_brief)
            company = db.query(Company).filter(Company.id == company_id).first()
            spec, discovery_outcomes = _attempt_compiled_capability_discovery(
                db,
                company=company,
                agent=agent,
                spec=spec,
            )
            spec = _carry_forward_requirement_bindings(previous_spec, spec)
            spec, _auto_bound = _auto_bind_single_packaged_integrations(
                db,
                company_id=company_id,
                spec=spec,
            )
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

    if _self_service_commercial_gating():
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
    elif has_entitlement or not _self_service_commercial_gating():
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
                discovery = (
                    requirement.get("discovery")
                    if isinstance(requirement.get("discovery"), dict)
                    else {}
                )
                discovery_status = str(discovery.get("status") or "")
                customer_access = str(discovery.get("customer_access") or "unknown")
                auth_schemes = [
                    item for item in (discovery.get("auth_schemes") or [])
                    if isinstance(item, dict)
                ]
                if discovery_status in {"contract_found", "operation_selection_required"} and customer_access in {"api_key", "account_connection"}:
                    scheme = auth_schemes[0] if len(auth_schemes) == 1 else {}
                    auth_type = str(scheme.get("auth_type") or "")
                    fields: list[dict] = []
                    oauth_interactive = False
                    if auth_type == "basic":
                        fields = [
                            {"key": "username", "label": "Username", "type": "text"},
                            {"key": "password", "label": "Password", "type": "password"},
                        ]
                    elif auth_type == "oauth":
                        flows = [
                            item for item in (scheme.get("flows") or [])
                            if isinstance(item, dict)
                        ]
                        oauth_interactive = any(
                            item.get("flow") == "authorization_code" for item in flows
                        )
                        if oauth_interactive or any(item.get("flow") == "client_credentials" for item in flows):
                            fields = [
                                {"key": "client_id", "label": "OAuth client ID", "type": "text"},
                                {"key": "client_secret", "label": "OAuth client secret", "type": "password"},
                            ]
                    elif auth_type in {"bearer", "api_key_header", "api_key_query"} or customer_access == "api_key":
                        fields = [{
                            "key": "api_key",
                            "label": "API key / token",
                            "type": "password",
                        }]
                    setup_actions.append(
                        _builder_action(
                            "provide_discovery_access",
                            f"Authorize {key.replace('_', ' ')}",
                            target="builder",
                            key=key,
                            detail=(
                                "Xvond already found and prepared the API contract. "
                                "Provide only the missing credential so Xvond can validate and finish the connection."
                            ),
                            fields=fields,
                            oauth_interactive=bool(auth_type == "oauth" and oauth_interactive),
                        )
                    )
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
                if schedule_status == "approval_required":
                    setup_actions.append(
                        _builder_action(
                            "set_permission",
                            f"Choose automation permission for {key.replace('_', ' ')}",
                            target="builder",
                            key=key,
                            detail=(
                                "Scheduled execution is ready except for owner permission. "
                                "Choose Automatic to let it run unattended, or change the "
                                "job so this action does not need background execution."
                            ),
                        )
                    )
                elif execution_status not in {"", "ready", "permission_denied"} or schedule_status not in {
                    "",
                    "ready",
                    "not_required",
                    "permission_denied",
                }:
                    waiting_reasons.append(
                        f"Xvond execution setup is still required for {key.replace('_', ' ')}."
                    )

        delivery = (
            compiled_spec.get("delivery")
            if isinstance(compiled_spec.get("delivery"), dict)
            else {}
        )
        graph_triggers = (
            [
                item
                for item in (delivery.get("graph_triggers") or [])
                if isinstance(item, dict)
            ]
            if isinstance(delivery.get("graph_triggers"), list)
            else []
        )
        if not graph_triggers and isinstance(delivery.get("graph_trigger"), dict):
            graph_triggers = [delivery["graph_trigger"]]

        for graph_trigger in graph_triggers:
            routine_id = str(
                graph_trigger.get("routine_id") or "primary"
            ).strip() or "primary"
            routine_name = str(
                graph_trigger.get("routine_name")
                or routine_id.replace("_", " ").title()
            ).strip()
            if (
                graph_trigger.get("trigger_type") == "webhook"
                and graph_trigger.get("status") == "ready"
                and graph_trigger.get("workflow_id")
            ):
                setup_actions.append(
                    _builder_action(
                        "setup_webhook",
                        f"Configure webhook for {routine_name}",
                        target="builder",
                        key=f"webhook_trigger:{routine_id}",
                        detail="Copy this routine's Xvond webhook URL and key into the external system that should trigger it.",
                    )
                )

            graph_trigger_status = str(
                graph_trigger.get("status") or "not_required"
            )
            if graph_trigger_status == "runtime_input_conflict":
                conflicts = ", ".join(
                    str(item)
                    for item in (graph_trigger.get("runtime_input_conflicts") or [])
                )
                waiting_reasons.append(
                    f"{routine_name} has conflicting runtime input keys"
                    + (f": {conflicts}." if conflicts else ".")
                )
            elif graph_trigger_status in {
                "schedule_required",
                "schedule_setup_required",
                "setup_required",
                "disabled",
            }:
                waiting_reasons.append(
                    f"Xvond execution trigger setup is not ready for {routine_name}."
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
    routine_required, routine_tested, _ = _routine_preview_evidence(
        builder,
        spec=compiled_spec or {},
    )
    routine_names = {
        str(item.get("id") or ""): str(item.get("name") or item.get("id") or "")
        for item in _compiled_execution_routines(compiled_spec or {})
    }
    untested_routines = [
        item for item in routine_required if item not in routine_tested
    ]

    setup_stage = next((item for item in stages if item.get("id") == "setup"), {})
    setup_complete = setup_stage.get("status") == "complete"
    if tested_build and setup_complete:
        add_stage(
            "test",
            "Preview & Test",
            "complete",
            (
                "Every executable routine in the current build passed a side-effect-free preview."
                if routine_required
                else "The current conversational employee build has been preview-tested safely."
            ),
        )
    elif provisioned and (has_entitlement or not _self_service_commercial_gating()) and setup_complete:
        if routine_required:
            preview_actions = [
                _builder_action(
                    "preview_routine",
                    f"Preview {routine_names.get(routine_id) or routine_id}",
                    target="builder",
                    key=routine_id,
                    detail="Run this routine safely without sending, publishing, booking, writing state or other business side effects.",
                )
                for routine_id in untested_routines
            ]
            add_stage(
                "test",
                "Preview & Test",
                "action_required",
                (
                    f"Preview every executable routine before launch "
                    f"({len(routine_tested)}/{len(routine_required)} tested). "
                    "Live business side effects are simulated."
                ),
                preview_actions,
            )
        else:
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
            (
                "Preview every current routine before launch."
                if routine_required
                else "Test the current employee build once before launch."
            ),
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
        "required_routines": routine_required,
        "tested_routines": routine_tested,
    }


@router.get("/employees")
def list_self_service_employees(
    current_user: User = Depends(require_customer_manager),
):
    """List employee projects in the customer's Replit-style workspace."""
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        rows = (
            db.query(AIAgent, AgentConfig)
            .join(AgentConfig, AgentConfig.agent_id == AIAgent.id)
            .filter(
                AIAgent.company_id == company.id,
                AgentConfig.agent_type == "employee",
            )
            .order_by(AIAgent.id.desc())
            .all()
        )
        employees = []
        for agent, config in rows:
            if not is_self_service_employee(company, config):
                continue
            builder = (config.settings or {}).get("employee_builder") or {}
            spec = builder.get("compiled_spec") if isinstance(builder, dict) else None
            employees.append({
                "agent_id": agent.id,
                "name": agent.name,
                "description": agent.description,
                "enabled": bool(agent.enabled),
                "lifecycle": "live" if agent.enabled else "draft",
                "compiled": isinstance(spec, dict),
                "updated_at": (
                    str(builder.get("compiled_at") or builder.get("updated_at") or "")
                    if isinstance(builder, dict)
                    else ""
                ),
            })
        return {"employees": employees}
    finally:
        db.close()


@router.get("/current")
def current_employee(
    agent_id: int | None = Query(default=None, ge=1),
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = _employee_for_company(db, current_user.company_id, agent_id)
        if agent is None:
            return {"employee": None}
        config = _employee_config_or_404(db, agent)
        builder = (config.settings or {}).get("employee_builder") or {}
        compiled_spec = builder.get("compiled_spec") if isinstance(builder, dict) else None
        has_entitlement = _has_ai_agents_entitlement(db, current_user.company_id)
        company = _company_or_404(db, current_user.company_id)
        self_service_state = None
        builder_journey = None
        self_service_agent = is_self_service_employee(company, config)
        if self_service_agent:
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
        if self_service_agent:
            display_channels = communication_channels(display_channels)

        pending_view = None
        pending = (
            builder.get("pending_revision")
            if isinstance(builder, dict)
            and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if pending is not None:
            pending_spec = pending.get("compiled_spec")
            if self_service_agent and isinstance(pending_spec, dict):
                pending_spec = self_service_spec_view(pending_spec)
            pending_compiled_at = str(pending.get("compiled_at") or "").strip()
            pending_tested = bool(
                pending_compiled_at
                and str(pending.get("last_tested_compiled_at") or "").strip()
                == pending_compiled_at
            )
            pending_view = {
                "status": pending.get("status") or "draft",
                "job_brief": pending.get("source_description"),
                "requested_channels": communication_channels(
                    pending.get("requested_channels") or []
                ),
                "compiled": isinstance(pending_spec, dict),
                "compiled_spec": pending_spec if isinstance(pending_spec, dict) else None,
                "compiled_at": pending.get("compiled_at"),
                "created_at": pending.get("created_at"),
                "updated_at": pending.get("updated_at"),
                "last_tested_at": pending.get("last_tested_at"),
                "current_build_tested": pending_tested,
                "can_apply": bool(agent.enabled and pending_tested),
            }
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
                "can_compile": bool(has_entitlement or self_service_agent),
                "delivery_mode": "self_service" if self_service_agent else "managed",
                "self_service_readiness": self_service_state,
                "builder_journey": builder_journey,
                "last_tested_at": builder.get("last_tested_at"),
                "versions": _builder_versions_view(builder),
                "current_build_tested": bool(
                    builder.get("compiled_at")
                    and builder.get("last_tested_compiled_at") == builder.get("compiled_at")
                ),
                "pending_revision": pending_view,
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
        # This customer-facing Builder always creates a Self-Service project.
        # Managed delivery is created and maintained through the admin flow.
        is_self_service = True

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
                "owner_permissions": {},
                "onboarding_source": "self_service",
                "delivery_mode": "self_service",
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
            enforce_capacity=(has_entitlement if not is_self_service else False),
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
            "subscription_required_for_go_live": bool(
                settings.SELF_SERVICE_REQUIRE_SUBSCRIPTION
                and not settings.SELF_SERVICE_FREE_EXPERIMENT
                and not has_entitlement
            ),
            "subscription_required_for_compile": bool(not has_entitlement and not is_self_service),
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
        agent, config = _self_service_employee_or_404(
            db,
            company=company,
            agent_id=agent_id,
        )

        # Keep the same lock order as compilation: config first, then agent.
        # This serializes revision with launch/build without creating a lock cycle.
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
                "owner_permissions": {},
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
        agent, config = _self_service_employee_or_404(
            db,
            company=company,
            agent_id=agent_id,
        )
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        builder = dict((config.settings or {}).get("employee_builder") or {})
        existing_pending = (
            builder.get("pending_revision")
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        current_brief = str(
            (existing_pending or {}).get("source_description")
            or builder.get("source_description")
            or agent.description
            or ""
        ).strip()
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

            now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
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
                "setup_answers": deepcopy(
                    (existing_pending or {}).get("setup_answers")
                    or builder.get("setup_answers")
                    or {}
                ),
                "base_compiled_at": (
                    (existing_pending or {}).get("base_compiled_at")
                    or builder.get("compiled_at")
                ),
                "created_at": (
                    (existing_pending or {}).get("created_at") or now_iso
                ),
                "updated_at": now_iso,
                "compiled_spec": None,
            }
            staged = _compile_staged_employee_spec(
                    db,
                    company_id=company.id,
                    agent=agent,
                    job_brief=blueprint.description,
                    requested_channels=pending["requested_channels"],
                    previous_spec=(
                        (existing_pending or {}).get("compiled_spec")
                        if isinstance((existing_pending or {}).get("compiled_spec"), dict)
                        else (
                            builder.get("compiled_spec")
                            if isinstance(builder.get("compiled_spec"), dict)
                            else None
                        )
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
    if settings.SELF_SERVICE_FREE_EXPERIMENT:
        return True
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
        agent, config = _self_service_employee_or_404(
            db,
            company=company,
            agent_id=agent_id,
        )
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
        agent, config = _self_service_employee_or_404(
            db,
            company=company,
            agent_id=agent_id,
        )
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

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

        restored_brief = str(selected.get("source_description") or "").strip()
        if not restored_brief:
            raise HTTPException(409, "Selected version has no Job Brief")
        restored_spec = selected.get("compiled_spec")
        restored_channels = communication_channels(
            selected.get("requested_channels") or []
        )
        restored_capabilities = dict(selected.get("capabilities") or {})

        if agent.enabled:
            now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            pending = {
                "version": 1,
                "status": "built" if isinstance(restored_spec, dict) else "draft",
                "source_description": restored_brief,
                "job_brief": restored_brief,
                "audience": selected.get("audience"),
                "requested_channels": restored_channels,
                "permissions": deepcopy(dict(selected.get("permissions") or {})),
                "capabilities": restored_capabilities,
                "setup_answers": deepcopy(dict(selected.get("setup_answers") or {})),
                "base_compiled_at": builder.get("compiled_at"),
                "created_at": now_iso,
                "updated_at": now_iso,
                "source_version_id": selected.get("id"),
                "compiled_spec": (
                    deepcopy(restored_spec)
                    if isinstance(restored_spec, dict)
                    else None
                ),
                "compiled_at": now_iso if isinstance(restored_spec, dict) else None,
            }
            if not isinstance(restored_spec, dict) and _has_ai_agents_entitlement(
                db, company.id
            ):
                staged = _compile_staged_employee_spec(
                    db,
                    company_id=company.id,
                    agent=agent,
                    job_brief=restored_brief,
                    requested_channels=restored_channels,
                    previous_spec=(
                        builder.get("compiled_spec")
                        if isinstance(builder.get("compiled_spec"), dict)
                        else None
                    ),
                )
                pending.update(staged)
                pending["status"] = "built"

            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": "rollback_staged",
                "agent_id": agent.id,
                "source_version_id": selected.get("id"),
                "compiled": isinstance(pending.get("compiled_spec"), dict),
                "live_employee_unchanged": True,
            }

        previous_capabilities = dict(config.capabilities or {})
        builder = _snapshot_builder_version(
            builder,
            reason="before_rollback",
            capabilities=previous_capabilities,
        )
        _clear_generated_self_service_build(
            db,
            company_id=company.id,
            agent_id=agent.id,
        )

        builder["source_description"] = restored_brief
        builder["job_brief"] = restored_brief
        builder["requested_channels"] = restored_channels
        builder["audience"] = selected.get("audience")
        builder["permissions"] = deepcopy(dict(selected.get("permissions") or {}))
        builder["setup_answers"] = deepcopy(dict(selected.get("setup_answers") or {}))
        builder["owner_permissions"] = deepcopy(
            dict(selected.get("owner_permissions") or {})
        )
        _clear_current_build_evidence(builder)

        if isinstance(restored_spec, dict):
            restored_spec = _effective_compiled_permissions(
                deepcopy(restored_spec),
                builder,
            )
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
            builder["owner_permissions"] = {}
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


def _bounded_connection_operations(value: dict | None) -> dict[str, dict]:
    """Validate owner/API-doc supplied operations for a generic connection."""
    result: dict[str, dict] = {}
    if not isinstance(value, dict):
        return result
    for raw_name, raw in value.items():
        name = normalize_requirement_key(raw_name)
        if not name or not isinstance(raw, dict):
            continue
        method = str(raw.get("method") or "POST").strip().upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise HTTPException(400, f"Unsupported HTTP method for operation {name}")
        endpoint = _relative_endpoint(raw.get("endpoint"), required=True)
        try:
            timeout = float(raw.get("timeout") or 15)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Invalid timeout for operation {name}")
        input_mode = str(
            raw.get("input_mode") or ("query" if method == "GET" else "json")
        ).strip().lower()
        if input_mode not in {"json", "json_array", "form", "multipart", "query", "none"}:
            raise HTTPException(400, f"Invalid input mode for operation {name}")
        path_params = list(dict.fromkeys(
            re.findall(r"{([A-Za-z_][A-Za-z0-9_]{0,63})}", endpoint)
        ))
        blocked_headers = {
            "authorization", "proxy-authorization", "cookie", "set-cookie",
            "host", "content-length", "transfer-encoding", "connection",
            "upgrade", "expect", "content-type", "idempotency-key",
            "x-xvond-idempotency-key",
        }
        def bounded_header_names(raw_values) -> list[str]:
            raw_values = raw_values if isinstance(raw_values, list) else []
            result_headers: list[str] = []
            for item in raw_values[:50]:
                value = str(item or "").strip()
                if (
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value)
                    and value.lower() not in blocked_headers
                    and value.lower() not in {x.lower() for x in result_headers}
                ):
                    result_headers.append(value)
            return result_headers
        header_params = bounded_header_names(raw.get("header_params"))
        required_header_params = bounded_header_names(raw.get("required_header_params"))
        for value in required_header_params:
            if value.lower() not in {x.lower() for x in header_params}:
                header_params.append(value)
        raw_query_params = raw.get("query_params")
        raw_query_params = raw_query_params if isinstance(raw_query_params, list) else []
        query_params = [
            str(item).strip()
            for item in raw_query_params
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.-]{0,63}",
                str(item or "").strip(),
            )
        ][:50]
        required_query_params = [
            str(item).strip()
            for item in (raw.get("required_query_params") or [])
            if re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.-]{0,63}",
                str(item or "").strip(),
            )
        ][:50]
        required_json_fields = [
            str(item).strip()
            for item in (raw.get("required_json_fields") or [])
            if re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}",
                str(item or "").strip(),
            )
        ][:50]
        json_fields: list[dict] = []
        raw_json_fields = raw.get("json_fields")
        raw_json_fields = raw_json_fields if isinstance(raw_json_fields, list) else []
        for item in raw_json_fields[:50]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", key):
                continue
            field = {
                "key": key,
                "required": bool(item.get("required")) or key in required_json_fields,
                "type": str(item.get("type") or "string").strip().lower()[:20],
            }
            fmt = str(item.get("format") or "").strip().lower()[:40]
            if fmt:
                field["format"] = fmt
            description = str(item.get("description") or "").strip()[:300]
            if description:
                field["description"] = description
            enum = item.get("enum")
            if isinstance(enum, list):
                bounded_enum = [
                    value for value in enum[:20]
                    if isinstance(value, (str, int, float, bool)) or value is None
                ]
                if bounded_enum:
                    field["enum"] = bounded_enum
            nested_schema = sanitize_json_contract(item.get("schema"))
            if nested_schema:
                field["schema"] = nested_schema
            json_fields.append(field)

        required_form_fields = [
            str(item).strip()
            for item in (raw.get("required_form_fields") or [])
            if re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}",
                str(item or "").strip(),
            )
        ][:50]
        form_fields: list[dict] = []
        raw_form_fields = raw.get("form_fields")
        raw_form_fields = raw_form_fields if isinstance(raw_form_fields, list) else []
        for item in raw_form_fields[:50]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", key):
                continue
            field = {
                "key": key,
                "required": bool(item.get("required")) or key in required_form_fields,
                "type": str(item.get("type") or "string").strip().lower()[:20],
            }
            fmt = str(item.get("format") or "").strip().lower()[:40]
            if fmt:
                field["format"] = fmt
            description = str(item.get("description") or "").strip()[:300]
            if description:
                field["description"] = description
            enum = item.get("enum")
            if isinstance(enum, list):
                bounded_enum = [
                    value for value in enum[:20]
                    if isinstance(value, (str, int, float, bool)) or value is None
                ]
                if bounded_enum:
                    field["enum"] = bounded_enum
            form_fields.append(field)
        for field in form_fields:
            if field["required"] and field["key"] not in required_form_fields:
                required_form_fields.append(field["key"])
                if len(required_form_fields) >= 50:
                    break

        for field in json_fields:
            if field["required"] and field["key"] not in required_json_fields:
                required_json_fields.append(field["key"])
                if len(required_json_fields) >= 50:
                    break

        array_item_kind = str(raw.get("array_item_kind") or "").strip().lower()
        if array_item_kind not in {"object", "string", "integer", "number", "boolean"}:
            array_item_kind = ""
        try:
            array_max_items = int(raw.get("array_max_items") or 100)
        except (TypeError, ValueError):
            array_max_items = 100
        array_max_items = max(1, min(array_max_items, 100))
        required_array_item_fields = [
            str(item).strip()
            for item in (raw.get("required_array_item_fields") or [])
            if re.fullmatch(
                r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}",
                str(item or "").strip(),
            )
        ][:50]

        response_status = str(raw.get("response_status") or "").strip()
        if not re.fullmatch(r"2[0-9][0-9]", response_status):
            response_status = ""
        response_kind = str(raw.get("response_kind") or "").strip().lower()
        if response_kind not in {
            "none", "object", "array", "string", "integer", "number", "boolean", "unknown"
        }:
            response_kind = ""
        response_item_kind = str(raw.get("response_item_kind") or "").strip().lower()
        if response_item_kind not in {
            "none", "object", "array", "string", "integer", "number", "boolean", "unknown"
        }:
            response_item_kind = ""

        def bounded_response_fields(field_name: str) -> list[dict]:
            raw_fields = raw.get(field_name)
            raw_fields = raw_fields if isinstance(raw_fields, list) else []
            bounded: list[dict] = []
            for item in raw_fields[:25]:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", key):
                    continue
                field = {
                    "key": key,
                    "required": bool(item.get("required")),
                    "type": str(item.get("type") or "string").strip().lower()[:20],
                }
                fmt = str(item.get("format") or "").strip().lower()[:40]
                if fmt:
                    field["format"] = fmt
                description = str(item.get("description") or "").strip()[:300]
                if description:
                    field["description"] = description
                enum = item.get("enum")
                if isinstance(enum, list):
                    bounded_enum = [
                        value for value in enum[:20]
                        if isinstance(value, (str, int, float, bool)) or value is None
                    ]
                    if bounded_enum:
                        field["enum"] = bounded_enum
                nested_schema = sanitize_json_contract(item.get("schema"))
                if nested_schema:
                    field["schema"] = nested_schema
                bounded.append(field)
            return bounded

        operation_result = {
            "method": method,
            "endpoint": endpoint,
            "input_mode": input_mode,
            "timeout": max(1, min(timeout, 30)),
            "path_params": path_params,
            "header_params": header_params,
            "required_header_params": required_header_params,
            "query_params": list(dict.fromkeys(query_params)),
            "required_query_params": list(dict.fromkeys(required_query_params)),
            "required_json_fields": list(dict.fromkeys(required_json_fields)),
            "json_fields": json_fields,
            "required_form_fields": list(dict.fromkeys(required_form_fields)),
            "form_fields": form_fields,
            "description": str(raw.get("description") or "").strip()[:500],
        }
        if input_mode == "json_array" and array_item_kind:
            operation_result["array_item_kind"] = array_item_kind
            operation_result["array_max_items"] = array_max_items
            if required_array_item_fields:
                operation_result["required_array_item_fields"] = list(
                    dict.fromkeys(required_array_item_fields)
                )
            array_item_fields = bounded_response_fields("array_item_fields")
            if array_item_fields:
                operation_result["array_item_fields"] = array_item_fields
        if response_status:
            operation_result["response_status"] = response_status
        if response_kind:
            operation_result["response_kind"] = response_kind
        response_fields = bounded_response_fields("response_fields")
        if response_fields:
            operation_result["response_fields"] = response_fields
        if response_item_kind:
            operation_result["response_item_kind"] = response_item_kind
        response_item_fields = bounded_response_fields("response_item_fields")
        if response_item_fields:
            operation_result["response_item_fields"] = response_item_fields
        result[name] = operation_result
        if len(result) >= 50:
            break
    return result


def _operation_match_tokens(value) -> set[str]:
    stop = {
        "a", "an", "the", "to", "for", "of", "and", "or", "api", "http",
        "action", "operation", "request", "execute", "employee", "integration",
        "system", "external", "data",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
        if len(token) > 1 and token not in stop
    }


def _resolve_bound_graph_operations(
    spec: dict,
    *,
    requirement_key: str,
    operations: dict,
    operation_map: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Bind graph action nodes to real imported API operations, fail-closed if ambiguous."""
    updated = deepcopy(spec)
    available = {
        normalize_requirement_key(key): dict(value)
        for key, value in (operations or {}).items()
        if normalize_requirement_key(key) and isinstance(value, dict)
    }
    explicit = {
        str(node_id).strip(): normalize_requirement_key(operation)
        for node_id, operation in (operation_map or {}).items()
        if str(node_id).strip() and normalize_requirement_key(operation)
    }
    unresolved: list[dict] = []

    def choose(node: dict, *, locator: str) -> str | None:
        params = node.get("params") if isinstance(node.get("params"), dict) else {}
        node_id = str(node.get("id") or "").strip()
        requested = normalize_requirement_key(params.get("operation"))
        if requested and requested in available:
            return requested
        forced = explicit.get(locator) or explicit.get(node_id)
        if forced:
            return forced if forced in available else None
        if len(available) == 1:
            return next(iter(available))
        context_parts = [
            node_id,
            node.get("label"),
            params.get("action_type"),
            (params.get("arguments") or {}).keys()
            if isinstance(params.get("arguments"), dict)
            else "",
        ]
        context_tokens = _operation_match_tokens(" ".join(
            " ".join(str(item) for item in part)
            if not isinstance(part, str) and hasattr(part, "__iter__")
            else str(part or "")
            for part in context_parts
        ))
        ranked: list[tuple[int, str]] = []
        for name, config in available.items():
            name_tokens = _operation_match_tokens(name.replace("_", " "))
            description_tokens = _operation_match_tokens(config.get("description"))
            score = (4 * len(context_tokens & name_tokens)) + len(
                context_tokens & description_tokens
            )
            ranked.append((score, name))
        ranked.sort(reverse=True)
        if ranked and ranked[0][0] > 0 and (
            len(ranked) == 1 or ranked[0][0] > ranked[1][0]
        ):
            return ranked[0][1]
        return None

    def visit(graph: dict, *, prefix: str) -> None:
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if not isinstance(nodes, list):
            return
        for index, raw in enumerate(nodes):
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("id") or f"node_{index + 1}").strip()
            locator = f"{prefix}/{node_id}" if prefix else node_id
            node_type = str(raw.get("type") or "").strip().lower()
            params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
            if (
                node_type == "action"
                and normalize_requirement_key(params.get("action_type"))
                == requirement_key
            ):
                selected = choose(raw, locator=locator)
                if selected:
                    params = dict(params)
                    params["operation"] = selected
                    raw["params"] = params
                else:
                    unresolved.append({
                        "node_id": node_id,
                        "locator": locator,
                        "label": str(raw.get("label") or "")[:200],
                        "available_operations": sorted(available),
                    })
            elif node_type in {"foreach", "repeat"}:
                nested = params.get("graph")
                if isinstance(nested, dict):
                    visit(nested, prefix=locator)

    graph = updated.get("execution_graph")
    if isinstance(graph, dict):
        visit(graph, prefix="primary")
    routines = updated.get("execution_routines")
    if isinstance(routines, list):
        for index, routine in enumerate(routines):
            if not isinstance(routine, dict):
                continue
            routine_id = normalize_requirement_key(
                routine.get("id") or routine.get("key") or f"routine_{index + 1}"
            ) or f"routine_{index + 1}"
            graph = (
                routine.get("graph")
                if isinstance(routine.get("graph"), dict)
                else routine.get("execution_graph")
            )
            if isinstance(graph, dict):
                visit(graph, prefix=routine_id)
    return updated, unresolved


def _relative_endpoint(value: str | None, *, required: bool = False) -> str | None:
    endpoint = str(value or "").strip()
    if not endpoint:
        if required:
            raise HTTPException(400, "Required integration endpoint is missing")
        return None
    if endpoint.startswith("//") or endpoint.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Integration operation endpoints must be relative paths")
    if ".." in endpoint.split("/"):
        raise HTTPException(400, "Integration operation endpoints cannot traverse parent paths")
    return "/" + endpoint.lstrip("/")


@router.post("/{agent_id}/discover/{requirement_key}")
def discover_self_service_capability(
    agent_id: int,
    requirement_key: str,
    current_user: User = Depends(require_customer_manager),
):
    """Resolve one unseen external capability into a verified executable API contract."""

    key = normalize_requirement_key(requirement_key)
    if not key:
        raise HTTPException(400, "Capability requirement key is invalid")

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Capability discovery is available only for Self-Service employees")

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if agent.enabled and pending is None:
            raise HTTPException(409, "Stage a live revision before discovering new capabilities")

        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee before capability discovery")

        updated = deepcopy(compiled_spec)
        requirements = [
            dict(item) if isinstance(item, dict) else item
            for item in (updated.get("requirements") or [])
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
            raise HTTPException(404, "Capability requirement not found")

        discovery = requirement.get("discovery")
        if not isinstance(discovery, dict) or discovery.get("needed") is not True:
            raise HTTPException(409, "This requirement does not have a pending discovery plan")

        result = discover_openapi_contract(discovery)
        if result.get("status") != "resolved" or not isinstance(result.get("contract"), dict):
            discovery = dict(discovery)
            discovery["status"] = "not_found"
            discovery["attempted"] = list(result.get("attempted") or [])[:20]
            requirement["discovery"] = discovery
            updated["requirements"] = requirements
            if isinstance(pending, dict):
                pending["compiled_spec"] = updated
                pending["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
                _invalidate_preview_evidence(pending)
                builder["pending_revision"] = pending
            else:
                builder["compiled_spec"] = updated
                _invalidate_preview_evidence(builder)
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": "not_found",
                "agent_id": agent.id,
                "requirement_key": key,
                "attempted": discovery["attempted"],
            }

        contract = dict(result["contract"])
        operations = _bounded_connection_operations(contract.get("operations") or {})
        if not operations:
            raise HTTPException(409, "Discovered API contract has no executable operations")

        discovery = dict(discovery)
        discovery.update(
            {
                "status": "contract_found",
                "source": result.get("source"),
                "docs_url": result.get("docs_url"),
                "contract_title": str(contract.get("title") or "")[:200],
                "base_url": str(contract.get("base_url") or "")[:1200],
                "auth_schemes": list(contract.get("auth_schemes") or [])[:10],
                "operation_count": len(operations),
                "attempted": list(result.get("attempted") or [])[:20],
            }
        )
        requirement["discovery"] = discovery
        requirement["integration_operations"] = operations
        requirement["requires_connection"] = True
        requirement["fulfillment_mode"] = "external_connection"
        requirement["status"] = "connection_required"
        requirement["delivery_mode"] = "connect_and_compose"

        auto_provisioned = False
        access_mode = str(discovery.get("customer_access") or "unknown").strip().lower()
        if access_mode == "none":
            evidence = public_api_probe(contract)
            base_url = str(contract.get("base_url") or "").strip().rstrip("/")
            if evidence and base_url:
                integration_config = {
                    "base_url": base_url,
                    "validation_endpoint": str(evidence.get("endpoint") or ""),
                    "auth_type": "none",
                    "operations": operations,
                    "_xvond_validation": evidence,
                    "_xvond_discovery": {
                        "source": result.get("source"),
                        "docs_url": result.get("docs_url"),
                        "requirement_key": key,
                    },
                }
                validate_integration_config("custom_api", integration_config)
                current = (
                    db.query(CompanyIntegration)
                    .filter(
                        CompanyIntegration.company_id == company.id,
                        CompanyIntegration.enabled.is_(True),
                    )
                    .count()
                )
                service_limits.check_current(
                    db,
                    company.id,
                    "ai_agents",
                    "integrations",
                    current,
                )
                integration = CompanyIntegration(
                    company_id=company.id,
                    integration_type="custom_api",
                    name=(
                        str(discovery.get("service_hint") or "").strip()
                        or str(contract.get("title") or "").strip()
                        or key.replace("_", " ").title()
                    )[:200],
                    config=integration_config,
                    enabled=True,
                )
                db.add(integration)
                db.flush()

                requirement["integration_id"] = integration.id
                requirement["integration_type"] = "custom_api"
                requirement["validation_required"] = True
                requirement["status"] = "xvond_build"
                requirement["delivery_mode"] = "compose"
                discovery["status"] = "resolved"
                discovery["auto_provisioned"] = True
                auto_provisioned = True

        updated["requirements"] = requirements
        if auto_provisioned:
            updated["setup_required"] = [
                item
                for item in (updated.get("setup_required") or [])
                if normalize_requirement_key(item) != key
            ]
        updated, unresolved = _resolve_bound_graph_operations(
            updated,
            requirement_key=key,
            operations=operations,
        )
        if auto_provisioned and unresolved:
            requirement = next(
                item
                for item in updated["requirements"]
                if isinstance(item, dict)
                and normalize_requirement_key(item.get("key")) == key
            )
            requirement["status"] = "connection_required"
            requirement["delivery_mode"] = "connect_and_compose"
            requirement.pop("integration_id", None)
            requirement.pop("integration_type", None)
            requirement["discovery"]["status"] = "operation_selection_required"
            auto_provisioned = False

        if isinstance(pending, dict):
            pending["compiled_spec"] = updated
            pending["status"] = "built"
            pending["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            _invalidate_preview_evidence(pending)
            builder["pending_revision"] = pending
        else:
            if auto_provisioned:
                updated, delivery = provision_compiled_capabilities(
                    db,
                    agent_id=agent.id,
                    spec=updated,
                )
                builder["delivery"] = delivery
                agent.system_prompt = build_compiled_employee_system_prompt(
                    owner_name=company.name,
                    spec=updated,
                )
            builder["compiled_spec"] = updated
            builder["missing_information"] = list(updated.get("setup_required") or [])
            _invalidate_preview_evidence(builder)

        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.commit()
        return {
            "status": "resolved" if auto_provisioned else "contract_found",
            "agent_id": agent.id,
            "requirement_key": key,
            "auto_provisioned": auto_provisioned,
            "customer_access": access_mode,
            "docs_url": result.get("docs_url"),
            "operation_count": len(operations),
            "unresolved_operations": unresolved,
        }
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



@router.post("/{agent_id}/discover/{requirement_key}/oauth/start")
def start_discovered_oauth_authorization(
    agent_id: int,
    requirement_key: str,
    data: EmployeeBuilderDiscoveryAccessRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Start provider-neutral OAuth authorization for a discovered API."""
    key = normalize_requirement_key(requirement_key)
    if not key:
        raise HTTPException(400, "Capability requirement key is invalid")
    client_id = str(data.client_id or "").strip()
    client_secret = str(data.client_secret or "")
    if not client_id or not client_secret:
        raise HTTPException(400, "OAuth client ID and client secret are required")
    if not settings.PUBLIC_BASE_URL:
        raise HTTPException(409, "PUBLIC_BASE_URL is required for OAuth authorization")

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "OAuth setup is available only for Self-Service employees")
        agent = (
            db.query(AIAgent)
            .filter(AIAgent.id == int(agent_id), AIAgent.company_id == company.id)
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        config = _employee_config_or_404(db, agent)
        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if agent.enabled and pending is None:
            raise HTTPException(409, "Stage a live revision before changing discovered connections")
        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee before authorizing this capability")

        requirement = next(
            (
                item
                for item in (compiled_spec.get("requirements") or [])
                if isinstance(item, dict)
                and normalize_requirement_key(item.get("key")) == key
            ),
            None,
        )
        if requirement is None:
            raise HTTPException(404, "Capability requirement not found")
        discovery = requirement.get("discovery")
        if not isinstance(discovery, dict):
            raise HTTPException(409, "This requirement has no discovered API contract")
        operations = _bounded_connection_operations(
            requirement.get("integration_operations")
            if isinstance(requirement.get("integration_operations"), dict)
            else {}
        )
        base_url = str(discovery.get("base_url") or "").strip().rstrip("/")
        if not base_url or not operations:
            raise HTTPException(409, "Discovered API contract is incomplete")

        oauth_schemes = [
            item
            for item in (discovery.get("auth_schemes") or [])
            if isinstance(item, dict)
            and str(item.get("auth_type") or "") == "oauth"
        ]
        if len(oauth_schemes) != 1:
            raise HTTPException(409, "A single OAuth scheme is required for generic authorization")
        flows = [
            item
            for item in (oauth_schemes[0].get("flows") or [])
            if isinstance(item, dict)
            and item.get("flow") == "authorization_code"
        ]
        if len(flows) != 1:
            raise HTTPException(409, "This API does not expose one supported authorization-code flow")

        # A retry for the same employee requirement supersedes the previous
        # disabled OAuth attempt. Removing it keeps setup idempotent and prevents
        # abandoned popups from consuming the customer's integration allowance.
        stale_pending = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.company_id == company.id,
                CompanyIntegration.integration_type == "custom_api",
                CompanyIntegration.enabled.is_(False),
            )
            .all()
        )
        for candidate in stale_pending:
            candidate_config = reveal_config(candidate.config or {})
            candidate_pending = (
                candidate_config.get("_xvond_oauth_pending")
                if isinstance(candidate_config.get("_xvond_oauth_pending"), dict)
                else {}
            )
            if (
                int(candidate_pending.get("agent_id") or 0) == agent.id
                and normalize_requirement_key(candidate_pending.get("requirement_key")) == key
            ):
                db.delete(candidate)
        db.flush()

        current = (
            db.query(CompanyIntegration)
            .filter(CompanyIntegration.company_id == company.id)
            .count()
        )
        service_limits.check_current(db, company.id, "ai_agents", "integrations", current)

        redirect_uri = (
            f"{settings.PUBLIC_BASE_URL}"
            f"/customer/employee-builder/oauth/callback"
        )
        integration_config = {
            "base_url": base_url,
            "operations": operations,
            "_xvond_oauth_pending": {
                "agent_id": agent.id,
                "requirement_key": key,
                "client_id": client_id,
                "client_secret": client_secret,
                "flow": flows[0],
                "redirect_uri": redirect_uri,
            },
            "_xvond_discovery": {
                "source": discovery.get("source"),
                "docs_url": discovery.get("docs_url"),
                "requirement_key": key,
            },
        }
        integration = CompanyIntegration(
            company_id=company.id,
            integration_type="custom_api",
            name=(
                str(discovery.get("service_hint") or "").strip()
                or str(discovery.get("contract_title") or "").strip()
                or key.replace("_", " ").title()
            )[:200],
            config=integration_config,
            enabled=False,
        )
        db.add(integration)
        db.flush()

        authorization = create_oauth_authorization(
            flows[0],
            client_id=client_id,
            redirect_uri=redirect_uri,
            state_secret=settings.GENERIC_OAUTH_STATE_SECRET,
            company_id=company.id,
            agent_id=agent.id,
            requirement_key=key,
            integration_id=integration.id,
        )
        integration_config["_xvond_oauth_pending"]["pkce_verifier_secret"] = authorization["code_verifier"]
        integration.config = integration_config
        db.commit()
        return {
            "status": "authorization_required",
            "authorization_url": authorization["authorization_url"],
            "expires_in": authorization["expires_in"],
        }
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/oauth/callback", response_class=HTMLResponse)
def finish_discovered_oauth_authorization(
    code: str | None = Query(default=None, max_length=8000),
    state: str | None = Query(default=None, max_length=20000),
    error: str | None = Query(default=None, max_length=1000),
    current_user: User = Depends(require_customer_manager),
):
    """Finish a signed generic OAuth authorization and attach it to the employee."""
    if error:
        return HTMLResponse(
            "<html><body><h3>Connection cancelled</h3>"
            "<script>if(window.opener){window.opener.postMessage({type:'xvond-oauth',status:'error'},window.location.origin);}window.close();</script>"
            "</body></html>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(400, "OAuth callback is missing code or state")

    db = SessionLocal()
    try:
        payload = consume_oauth_state(
            state,
            state_secret=settings.GENERIC_OAUTH_STATE_SECRET,
        )
        company_id = int(payload.get("company_id") or 0)
        if company_id != int(current_user.company_id):
            raise HTTPException(403, "OAuth state does not belong to the current customer")
        agent_id = int(payload.get("agent_id") or 0)
        integration_id = int(payload.get("integration_id") or 0)
        key = normalize_requirement_key(payload.get("requirement_key"))
        if not company_id or not agent_id or not integration_id or not key:
            raise HTTPException(400, "OAuth state is incomplete")

        integration = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
            )
            .first()
        )
        if integration is None:
            raise HTTPException(404, "Pending OAuth integration was not found")
        raw_config = reveal_config(integration.config or {})
        pending_oauth = (
            raw_config.get("_xvond_oauth_pending")
            if isinstance(raw_config.get("_xvond_oauth_pending"), dict)
            else {}
        )
        if (
            int(pending_oauth.get("agent_id") or 0) != agent_id
            or normalize_requirement_key(pending_oauth.get("requirement_key")) != key
        ):
            raise HTTPException(409, "OAuth state does not match the pending integration")

        token = exchange_authorization_code(
            state_payload=payload,
            code=code,
            client_secret=str(pending_oauth.get("client_secret") or ""),
            code_verifier=str(pending_oauth.get("pkce_verifier_secret") or ""),
        )
        operations = _bounded_connection_operations(raw_config.get("operations") or {})
        base_url = str(raw_config.get("base_url") or "").strip().rstrip("/")
        auth_config = {
            "auth_type": "bearer",
            "api_key": token["access_token"],
        }
        evidence = api_connection_probe(
            {"base_url": base_url, "operations": operations},
            auth_config=auth_config,
        )
        if not evidence:
            raise HTTPException(
                409,
                "Xvond could not safely validate the authorized account with a read-only operation",
            )

        token_timing = oauth_token_timing(token.get("expires_in"))
        integration.config = {
            "base_url": base_url,
            "validation_endpoint": str(evidence.get("endpoint") or ""),
            "operations": operations,
            "_xvond_validation": evidence,
            "_xvond_discovery": raw_config.get("_xvond_discovery") or {},
            "auth_type": "bearer",
            "api_key": token["access_token"],
            "_xvond_oauth": {
                "flow": "authorization_code",
                "token_url": payload.get("token_url"),
                "scopes": (pending_oauth.get("flow") or {}).get("scopes") or [],
                "client_id": pending_oauth.get("client_id"),
                "client_secret": pending_oauth.get("client_secret"),
                "refresh_token": token.get("refresh_token"),
                "expires_in": token.get("expires_in"),
                "scope": token.get("scope"),
                **token_timing,
            },
        }
        integration.enabled = True

        agent = (
            db.query(AIAgent)
            .filter(AIAgent.id == agent_id, AIAgent.company_id == company_id)
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Employee build state is missing")

        updated = deepcopy(compiled_spec)
        requirements = [
            dict(item) if isinstance(item, dict) else item
            for item in (updated.get("requirements") or [])
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
            raise HTTPException(404, "Capability requirement not found")

        requirement["integration_id"] = integration.id
        requirement["integration_type"] = "custom_api"
        requirement["integration_operations"] = operations
        requirement["fulfillment_mode"] = "external_connection"
        requirement["validation_required"] = True
        requirement["requires_connection"] = True
        requirement["status"] = "xvond_build"
        requirement["delivery_mode"] = "compose"
        discovery = dict(requirement.get("discovery") or {})
        discovery["status"] = "resolved"
        discovery["credential_configured"] = True
        requirement["discovery"] = discovery

        updated["requirements"] = requirements
        updated["setup_required"] = [
            item
            for item in (updated.get("setup_required") or [])
            if normalize_requirement_key(item) != key
        ]
        updated, unresolved = _resolve_bound_graph_operations(
            updated,
            requirement_key=key,
            operations=operations,
        )
        if unresolved:
            raise HTTPException(
                409,
                detail={
                    "error": "api_operation_selection_required",
                    "requirement_key": key,
                    "unresolved": unresolved,
                },
            )

        if isinstance(pending, dict):
            pending["compiled_spec"] = updated
            pending["status"] = "built"
            pending["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            _invalidate_preview_evidence(pending)
            builder["pending_revision"] = pending
        else:
            updated, delivery = provision_compiled_capabilities(
                db,
                agent_id=agent.id,
                spec=updated,
            )
            builder["compiled_spec"] = updated
            builder["delivery"] = delivery
            builder["missing_information"] = list(updated.get("setup_required") or [])
            _invalidate_preview_evidence(builder)
            company = _company_or_404(db, company_id)
            agent.system_prompt = build_compiled_employee_system_prompt(
                owner_name=company.name,
                spec=updated,
            )

        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.commit()
        return HTMLResponse(
            "<html><body><h3>Account connected</h3>"
            "<script>if(window.opener){window.opener.postMessage({type:'xvond-oauth',status:'connected'},window.location.origin);}window.close();</script>"
            "</body></html>"
        )
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/discover/{requirement_key}/access")
def provide_discovered_capability_access(
    agent_id: int,
    requirement_key: str,
    data: EmployeeBuilderDiscoveryAccessRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Provide only the credential missing from a discovered API capability."""

    key = normalize_requirement_key(requirement_key)
    if not key:
        raise HTTPException(400, "Capability requirement key is invalid")

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Capability access setup is available only for Self-Service employees")

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if agent.enabled and pending is None:
            raise HTTPException(409, "Stage a live revision before changing discovered connections")

        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee before providing capability access")

        updated = deepcopy(compiled_spec)
        requirements = [
            dict(item) if isinstance(item, dict) else item
            for item in (updated.get("requirements") or [])
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
            raise HTTPException(404, "Capability requirement not found")

        discovery = requirement.get("discovery")
        if not isinstance(discovery, dict):
            raise HTTPException(409, "This requirement has no discovered API contract")
        if str(discovery.get("status") or "") not in {
            "contract_found",
            "operation_selection_required",
        }:
            raise HTTPException(409, "Discover the API contract before providing access")

        base_url = str(discovery.get("base_url") or "").strip().rstrip("/")
        operations = _bounded_connection_operations(
            requirement.get("integration_operations")
            if isinstance(requirement.get("integration_operations"), dict)
            else {}
        )
        if not base_url or not operations:
            raise HTTPException(409, "Discovered API contract is incomplete")

        schemes = [
            item
            for item in (discovery.get("auth_schemes") or [])
            if isinstance(item, dict)
            and str(item.get("auth_type") or "") in {
                "bearer", "api_key_header", "api_key_query", "basic", "oauth"
            }
        ]
        if len(schemes) > 1:
            raise HTTPException(
                409,
                detail={
                    "error": "auth_scheme_selection_required",
                    "schemes": schemes,
                    "message": "The API exposes multiple authentication schemes; choose the intended provider authorization method.",
                },
            )

        access_mode = str(discovery.get("customer_access") or "unknown").strip().lower()
        scheme = dict(schemes[0]) if schemes else {}
        auth_type = str(scheme.get("auth_type") or "").strip().lower()
        if not auth_type and access_mode == "api_key":
            auth_type = "bearer"
        if not auth_type:
            raise HTTPException(
                409,
                "Xvond could not determine a safe generic authentication scheme from the API contract",
            )

        auth_config: dict = {"auth_type": auth_type}
        if auth_type in {"bearer", "api_key_header", "api_key_query"}:
            token = str(data.api_key or "").strip()
            if not token:
                raise HTTPException(400, "API key or token is required")
            auth_config["api_key"] = token
            if auth_type in {"api_key_header", "api_key_query"}:
                name = str(scheme.get("api_key_name") or "").strip()
                if not name:
                    raise HTTPException(409, "API key location is missing from the discovered contract")
                auth_config["api_key_name"] = name
        elif auth_type == "basic":
            username = str(data.username or "").strip()
            password = str(data.password or "")
            if not username or not password:
                raise HTTPException(400, "Username and password are required")
            auth_config["username"] = username
            auth_config["password"] = password
        elif auth_type == "oauth":
            flows = [
                item for item in (scheme.get("flows") or [])
                if isinstance(item, dict)
            ]
            client_flow = next(
                (item for item in flows if item.get("flow") == "client_credentials"),
                None,
            )
            if client_flow is None:
                raise HTTPException(
                    409,
                    detail={
                        "error": "oauth_authorization_required",
                        "message": (
                            "This discovered API requires an interactive OAuth authorization. "
                            "Xvond will not invent or accept a raw OAuth token."
                        ),
                        "authorization_flows": flows,
                    },
                )
            client_id = str(data.client_id or "").strip()
            client_secret = str(data.client_secret or "")
            if not client_id or not client_secret:
                raise HTTPException(400, "OAuth client ID and client secret are required")
            token = oauth_client_credentials_token(
                client_flow,
                client_id=client_id,
                client_secret=client_secret,
            )
            auth_config = {
                "auth_type": "bearer",
                "api_key": token["access_token"],
                "_xvond_oauth": {
                    "flow": "client_credentials",
                    "token_url": client_flow.get("token_url"),
                    "scopes": client_flow.get("scopes") or [],
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "expires_in": token.get("expires_in"),
                    **oauth_token_timing(token.get("expires_in")),
                },
            }

        contract = {
            "base_url": base_url,
            "operations": operations,
        }
        evidence = api_connection_probe(contract, auth_config=auth_config)
        if not evidence:
            raise HTTPException(
                409,
                "Xvond could not safely validate these credentials using a read-only API operation",
            )

        integration_config = {
            "base_url": base_url,
            "validation_endpoint": str(evidence.get("endpoint") or ""),
            "operations": operations,
            "_xvond_validation": evidence,
            "_xvond_discovery": {
                "source": discovery.get("source"),
                "docs_url": discovery.get("docs_url"),
                "requirement_key": key,
            },
            **auth_config,
        }
        validate_integration_config("custom_api", integration_config)

        current = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.company_id == company.id,
                CompanyIntegration.enabled.is_(True),
            )
            .count()
        )
        service_limits.check_current(
            db,
            company.id,
            "ai_agents",
            "integrations",
            current,
        )

        integration = CompanyIntegration(
            company_id=company.id,
            integration_type="custom_api",
            name=(
                str(discovery.get("service_hint") or "").strip()
                or str(discovery.get("contract_title") or "").strip()
                or key.replace("_", " ").title()
            )[:200],
            config=integration_config,
            enabled=True,
        )
        db.add(integration)
        db.flush()

        requirement["integration_id"] = integration.id
        requirement["integration_type"] = "custom_api"
        requirement["integration_operations"] = operations
        requirement["fulfillment_mode"] = "external_connection"
        requirement["validation_required"] = True
        requirement["requires_connection"] = True
        requirement["status"] = "xvond_build"
        requirement["delivery_mode"] = "compose"
        discovery = dict(discovery)
        discovery["status"] = "resolved"
        discovery["credential_configured"] = True
        requirement["discovery"] = discovery

        updated["requirements"] = requirements
        updated["setup_required"] = [
            item
            for item in (updated.get("setup_required") or [])
            if normalize_requirement_key(item) != key
        ]
        updated, unresolved = _resolve_bound_graph_operations(
            updated,
            requirement_key=key,
            operations=operations,
        )
        if unresolved:
            db.delete(integration)
            raise HTTPException(
                409,
                detail={
                    "error": "api_operation_selection_required",
                    "requirement_key": key,
                    "unresolved": unresolved,
                },
            )

        if isinstance(pending, dict):
            pending["compiled_spec"] = updated
            pending["status"] = "built"
            pending["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            _invalidate_preview_evidence(pending)
            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": "connected_to_pending_revision",
                "agent_id": agent.id,
                "requirement_key": key,
                "credential_type": auth_type,
                "live_employee_unchanged": True,
            }

        updated, delivery = provision_compiled_capabilities(
            db,
            agent_id=agent.id,
            spec=updated,
        )
        builder["compiled_spec"] = updated
        builder["delivery"] = delivery
        builder["missing_information"] = list(updated.get("setup_required") or [])
        _invalidate_preview_evidence(builder)
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        agent.system_prompt = build_compiled_employee_system_prompt(
            owner_name=company.name,
            spec=updated,
        )
        db.commit()
        return {
            "status": "connected",
            "agent_id": agent.id,
            "requirement_key": key,
            "credential_type": auth_type,
            "integration_id": integration.id,
        }
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/connections/auto-resolve")
def auto_resolve_self_service_integrations(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    """Reuse unambiguous validated packaged connections after they are added."""

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(
                409,
                "Automatic connection resolution is available only for Self-Service employees",
            )

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if agent.enabled and pending is None:
            return {
                "status": "unchanged",
                "agent_id": agent.id,
                "bound_requirements": [],
                "live_employee_unchanged": True,
            }

        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            return {
                "status": "unchanged",
                "agent_id": agent.id,
                "bound_requirements": [],
                "live_employee_unchanged": bool(agent.enabled),
            }

        resolved_spec, bound = _auto_bind_single_packaged_integrations(
            db,
            company_id=company.id,
            spec=compiled_spec,
        )
        if not bound:
            return {
                "status": "unchanged",
                "agent_id": agent.id,
                "bound_requirements": [],
                "live_employee_unchanged": bool(agent.enabled),
            }

        if isinstance(pending, dict):
            pending["compiled_spec"] = resolved_spec
            pending["status"] = "built"
            pending["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            _invalidate_preview_evidence(pending)
            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": "resolved_pending_revision",
                "agent_id": agent.id,
                "bound_requirements": bound,
                "live_employee_unchanged": True,
            }

        resolved_spec, delivery = provision_compiled_capabilities(
            db,
            agent_id=agent.id,
            spec=resolved_spec,
        )
        builder["compiled_spec"] = resolved_spec
        builder["delivery"] = delivery
        builder["missing_information"] = list(
            resolved_spec.get("setup_required") or []
        )
        _invalidate_preview_evidence(builder)
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        agent.system_prompt = build_compiled_employee_system_prompt(
            owner_name=company.name,
            spec=resolved_spec,
        )
        db.commit()
        return {
            "status": "resolved",
            "agent_id": agent.id,
            "bound_requirements": bound,
            "live_employee_unchanged": False,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
        if not _agent_id_is_self_service(db, company, agent_id):
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
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if agent.enabled and pending is None:
            raise HTTPException(
                409,
                "Stage a live revision before changing connected systems",
            )
        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee revision before connecting a system")

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

        executable_types = executable_integration_types()
        compatible_types = compatible_integration_types(key)
        if integration.integration_type not in executable_types:
            raise HTTPException(
                409,
                "This connected-system type does not have a real execution adapter yet.",
            )
        if integration.integration_type not in compatible_types:
            raise HTTPException(
                409,
                f"{key.replace('_', ' ').title()} requires a compatible Xvond connector.",
            )
        if key == "booking" and integration.integration_type == "webhook":
            raise HTTPException(
                409,
                "Booking needs a two-way API so Xvond can verify availability before creating the booking",
            )

        compiled_operations = requirement.get("integration_operations")
        compiled_operations = (
            _bounded_connection_operations(compiled_operations)
            if isinstance(compiled_operations, dict)
            else {}
        )
        configured_operations = _bounded_connection_operations(
            integration_config.get("operations")
            if isinstance(integration_config, dict)
            else {}
        )
        supplied_operations = _bounded_connection_operations(data.operations)
        execute_required = integration_requires_operation_endpoints(
            integration.integration_type
        )
        # A generic connector is complete when the compiler/API docs already
        # supplied one or more concrete operations; do not force a redundant
        # legacy /execute endpoint.
        legacy_execute_required = (
            execute_required
            and not compiled_operations
            and not configured_operations
            and not supplied_operations
        )
        execute_endpoint = _relative_endpoint(
            data.execute_endpoint,
            required=legacy_execute_required,
        )
        availability_endpoint = _relative_endpoint(data.availability_endpoint)
        cancel_endpoint = _relative_endpoint(data.cancel_endpoint)

        operations = integration_packaged_operations(
            integration.integration_type
        )
        operations.update(configured_operations)
        operations.update(compiled_operations)
        operations.update(supplied_operations)
        if execute_endpoint:
            operations["execute"] = {"method": "POST", "endpoint": execute_endpoint}
        if availability_endpoint:
            operations["availability"] = {
                "method": "POST",
                "endpoint": availability_endpoint,
            }
        if cancel_endpoint:
            operations["cancel"] = {"method": "POST", "endpoint": cancel_endpoint}

        # Keep default graph actions runnable without guessing across ambiguous
        # API docs. Exact requirement matches and a single imported operation
        # are deterministic enough to alias as the conventional execute action.
        if "execute" not in operations:
            alias_key = key if isinstance(operations.get(key), dict) else None
            if alias_key is None:
                concrete = [
                    operation_key
                    for operation_key, operation_value in operations.items()
                    if isinstance(operation_value, dict)
                ]
                if len(concrete) == 1:
                    alias_key = concrete[0]
            if alias_key:
                operations["execute"] = dict(operations[alias_key])

        if key == "booking" and execute_required:
            if not isinstance(operations.get("availability"), dict):
                raise HTTPException(
                    400,
                    "Booking systems need an availability operation so the employee can check real slots",
                )
            if not isinstance(operations.get("execute"), dict):
                raise HTTPException(
                    400,
                    "Booking systems need a booking/create operation",
                )

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
        compiled_value, unresolved_operations = _resolve_bound_graph_operations(
            compiled_value,
            requirement_key=key,
            operations=operations,
            operation_map=data.operation_map,
        )
        if unresolved_operations:
            raise HTTPException(
                409,
                detail={
                    "error": "api_operation_selection_required",
                    "requirement_key": key,
                    "message": (
                        "Xvond found multiple API operations and could not safely "
                        "choose one for every employee action."
                    ),
                    "unresolved": unresolved_operations,
                },
            )

        if isinstance(pending, dict):
            compiled_value["setup_required"] = [
                item
                for item in (compiled_value.get("setup_required") or [])
                if normalize_requirement_key(item) != key
            ]
            pending["compiled_spec"] = compiled_value
            _invalidate_preview_evidence(pending)
            pending["status"] = "built"
            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": "connected_to_pending_revision",
                "agent_id": agent.id,
                "requirement_key": key,
                "integration": {
                    "id": integration.id,
                    "name": integration.name,
                    "type": integration.integration_type,
                },
                "compiled_spec": self_service_spec_view(compiled_value),
                "live_employee_unchanged": True,
            }

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
        _invalidate_preview_evidence(builder)
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
        if not _agent_id_is_self_service(db, company, agent_id):
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

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        if agent.enabled and pending is None:
            raise HTTPException(
                409,
                "Stage a live revision before changing setup data",
            )
        compiled_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            else builder.get("compiled_spec")
        )
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee revision before providing setup data")

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

        answers = dict(
            (pending.get("setup_answers") or {})
            if isinstance(pending, dict)
            else (builder.get("setup_answers") or {})
        )
        answers[key] = answer_value
        if isinstance(pending, dict):
            pending["setup_answers"] = answers
        else:
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

        if isinstance(pending, dict):
            compiled_value["setup_required"] = [
                item
                for item in (compiled_value.get("setup_required") or [])
                if normalize_requirement_key(item) != key
            ]
            pending["compiled_spec"] = compiled_value
            _invalidate_preview_evidence(pending)
            pending["status"] = "built"
            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "status": "saved_to_pending_revision",
                "agent_id": agent.id,
                "requirement_key": key,
                "compiled_spec": self_service_spec_view(compiled_value),
                "live_employee_unchanged": True,
            }

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
        _invalidate_preview_evidence(builder)
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


@router.put("/{agent_id}/permissions/{requirement_key}")
def set_self_service_permission(
    agent_id: int,
    requirement_key: str,
    data: EmployeeBuilderPermissionRequest,
    current_user: User = Depends(require_customer_admin),
):
    """Set an explicit owner/admin grant for one executable employee capability."""

    key = normalize_requirement_key(requirement_key)
    mode = str(data.mode or "").strip().lower()
    if not key or len(key) > 120:
        raise HTTPException(400, "Permission requirement key is invalid")
    if mode not in OWNER_PERMISSION_MODES:
        raise HTTPException(
            400,
            "Permission mode must be automatic, ask_before, or never",
        )

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(
                409,
                "Owner permissions are available only for Self-Service employees",
            )

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        # Keep the same lock order as compilation/revision.
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        compiled_spec = builder.get("compiled_spec")
        if not isinstance(compiled_spec, dict):
            raise HTTPException(409, "Build the employee before changing permissions")

        requirement = next(
            (
                item
                for item in (compiled_spec.get("requirements") or [])
                if isinstance(item, dict)
                and normalize_requirement_key(item.get("key")) == key
            ),
            None,
        )
        if requirement is None:
            raise HTTPException(
                404,
                "Permission requirement not found in the current Job Brief",
            )

        action_plan = (
            (compiled_spec.get("delivery") or {}).get("action_plan")
            if isinstance(compiled_spec.get("delivery"), dict)
            else {}
        )
        if not isinstance(action_plan, dict) or key not in action_plan:
            raise HTTPException(
                409,
                "This requirement does not expose an executable action permission",
            )

        current_mode = "ask_before"
        for permission in compiled_spec.get("permissions") or []:
            if not isinstance(permission, dict):
                continue
            if normalize_requirement_key(permission.get("action")) == key:
                candidate = str(
                    permission.get("mode") or "ask_before"
                ).strip().lower()
                if candidate in OWNER_PERMISSION_MODES:
                    current_mode = candidate
                break

        if agent.enabled and mode == "automatic" and current_mode != "automatic":
            raise HTTPException(
                409,
                detail={
                    "message": (
                        "Pause this employee before granting automatic execution. "
                        "After the permission change, preview-test the updated build "
                        "before launching it again."
                    ),
                    "requires_deactivation": True,
                },
            )

        owner_permissions = dict(builder.get("owner_permissions") or {})
        stored_mode = str(owner_permissions.get(key) or "").strip().lower()
        if current_mode == mode and stored_mode == mode:
            return {
                "status": "unchanged",
                "agent_id": agent.id,
                "requirement_key": key,
                "mode": mode,
                "compiled_spec": self_service_spec_view(compiled_spec),
                "readiness": self_service_readiness(
                    db,
                    company=company,
                    agent=agent,
                    config=config,
                ),
            }

        builder = _snapshot_builder_version(
            builder,
            reason=f"permission:{key}:{mode}",
            capabilities=dict(config.capabilities or {}),
        )
        owner_permissions = dict(builder.get("owner_permissions") or {})
        owner_permissions[key] = mode
        builder["owner_permissions"] = owner_permissions

        # Permission changes alter executable behavior and therefore define a
        # new build identity. Restrictive changes may be applied while live;
        # automatic escalation was blocked above and must be preview-tested.
        builder["compiled_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _invalidate_preview_evidence(builder)

        compiled_value = _store_provisioned_spec(
            db,
            company_id=company.id,
            agent=agent,
            config=config,
            settings=settings_value,
            builder=builder,
            spec=compiled_spec,
        )

        db.commit()
        return {
            "status": "saved",
            "agent_id": agent.id,
            "requirement_key": key,
            "mode": mode,
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
    """Turn an open-ended Job Brief into a structured employee specification.

    Self-Service preview builds intentionally do not require a commercial
    subscription. Runtime/go-live entitlement remains enforced separately.
    """
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
        if not is_self_service_employee(company, config):
            service_limits.entitlement(db, current_user.company_id, "ai_agents")
            limits_service.check_token_limit(db, current_user.company_id)

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
            "discovery": _compiled_discovery_summary(compiled_spec),
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
        if not _agent_id_is_self_service(db, company, agent_id):
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
        if _self_service_commercial_gating():
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
                if _self_service_commercial_gating():
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
        if not _agent_id_is_self_service(db, company, agent_id):
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


@router.post("/{agent_id}/preview-routine")
def preview_employee_routine(
    agent_id: int,
    data: EmployeeBuilderRoutinePreviewRequest,
    current_user: User = Depends(require_customer_manager),
):
    """Safely execute one compiled routine without business side effects."""

    if len(data.input_data) > 100:
        raise HTTPException(400, "Routine preview input has too many fields")
    if len(data.simulated_outputs) > 100 or len(data.event_payloads) > 100:
        raise HTTPException(400, "Routine preview simulation has too many node values")

    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(
                409,
                "Routine preview is available only for Self-Service employees",
            )
        if not _has_ai_agents_entitlement(db, company.id):
            raise HTTPException(
                403,
                detail={
                    "message": "Subscribe to preview executable employee routines",
                    "subscription_required": True,
                },
            )

        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)

        settings_value = deepcopy(dict(config.settings or {}))
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        target = pending if isinstance(pending, dict) else builder
        spec = target.get("compiled_spec")
        if not isinstance(spec, dict):
            raise HTTPException(409, "Build the employee before previewing a routine")

        routines = _compiled_execution_routines(spec)
        if not routines:
            raise HTTPException(
                409,
                "This employee has no executable routine; use the chat preview instead",
            )

        requested_id = normalize_requirement_key(data.routine_id)
        routine = next(
            (item for item in routines if item.get("id") == requested_id),
            None,
        )
        if routine is None:
            raise HTTPException(
                404,
                detail={
                    "message": "Employee routine not found",
                    "available_routines": [
                        {
                            "routine_id": item.get("id"),
                            "routine_name": item.get("name"),
                        }
                        for item in routines
                    ],
                },
            )

        graph = normalize_execution_graph(routine.get("graph") or {})
        errors = graph_contract_errors(graph, graph_agent_id=agent.id)
        if errors:
            raise HTTPException(
                409,
                detail={
                    "message": "Routine execution contract is invalid",
                    "routine_id": requested_id,
                    "errors": errors,
                },
            )

        defaults = _routine_preview_defaults(spec, routine)
        preview_input = {
            **defaults,
            **deepcopy(data.input_data),
        }
        compiled_at = str(target.get("compiled_at") or "").strip()
        preview_system_prompt = build_compiled_employee_system_prompt(
            owner_name=company.name,
            spec=spec,
        )

        def preview_ai_executor(*, prompt: str, context, node_scope: str) -> dict:
            import json

            message = str(prompt or "").strip()
            if context is not None:
                try:
                    context_text = json.dumps(
                        context,
                        ensure_ascii=False,
                        default=str,
                    )
                except (TypeError, ValueError):
                    context_text = str(context)
                if context_text:
                    message = (message + "\n\nCONTEXT:\n" + context_text)[:12000]
            if not message:
                raise HTTPException(409, f"Preview AI node {node_scope} has no prompt")

            if _self_service_commercial_gating():
                limits_service.check_token_limit(db, company.id)
            selections = runtime_selections(
                db,
                company.id,
                agent.provider,
                agent.model,
                message=message,
            )
            if not selections:
                raise HTTPException(503, "No eligible AI provider/model is available")

            response = None
            selected = None
            for candidate in selections:
                try:
                    response = ai_engine.generate(
                        provider_name=candidate.provider,
                        system_prompt=preview_system_prompt,
                        user_message=message,
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
                company_id=company.id,
                agent_id=agent.id,
                selected=selected,
                response=response,
            )
            return {
                "ai_response": response.text,
                "node_scope": node_scope,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "total_tokens": response.total_tokens,
                },
            }

        preview_state = {
            **preview_input,
            "_xvond_preview": True,
            "_xvond_execution_key": (
                f"preview:{company.id}:{agent.id}:{requested_id}:{compiled_at or 'unversioned'}"
            ),
            "_xvond_preview_outputs": deepcopy(data.simulated_outputs),
            "_xvond_preview_event_payloads": deepcopy(data.event_payloads),
            "_xvond_preview_ai_executor": preview_ai_executor,
        }

        result = automation_runtime.execute_step(
            db,
            company.id,
            {
                "type": "graph",
                "agent_id": agent.id,
                "graph": graph,
            },
            preview_state,
            run_id=0,
            step_index=0,
        )

        required, tested, complete = _routine_preview_evidence(
            target,
            spec=spec,
            routine_id=requested_id,
            record=True,
        )
        if isinstance(pending, dict):
            builder["pending_revision"] = target
            test_target = "pending_revision"
        else:
            builder = target
            test_target = "current_build"

        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.commit()

        return {
            "status": "previewed",
            "agent_id": agent.id,
            "routine_id": requested_id,
            "routine_name": routine.get("name"),
            "test_target": test_target,
            "result": result,
            "required_routines": required,
            "tested_routines": tested,
            "current_build_tested": complete,
            "safety": {
                "business_actions_executed": False,
                "notifications_persisted": False,
                "state_mutated": False,
                "interactive_browser_executed": False,
                "media_generated": False,
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
        if agent.enabled:
            db.refresh(config, with_for_update=True)
            db.refresh(agent, with_for_update=True)

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = (
            deepcopy(builder.get("pending_revision"))
            if agent.enabled and isinstance(builder.get("pending_revision"), dict)
            else None
        )
        pending_spec = (
            pending.get("compiled_spec")
            if isinstance(pending, dict)
            and isinstance(pending.get("compiled_spec"), dict)
            else None
        )
        if pending_spec is not None:
            if _self_service_commercial_gating():
                if not _has_ai_agents_entitlement(db, current_user.company_id):
                    raise HTTPException(
                        403,
                        detail={
                            "message": "Subscribe to test a staged live revision",
                            "subscription_required": True,
                        },
                    )
                limits_service.check_token_limit(db, current_user.company_id)
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
            staged_prompt = build_compiled_employee_system_prompt(
                owner_name=company.name,
                spec=pending_spec,
            )
            for candidate in selections:
                try:
                    response = ai_engine.generate(
                        provider_name=candidate.provider,
                        system_prompt=staged_prompt,
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
            now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            pending["chat_tested_at"] = now_iso
            pending["chat_tested_compiled_at"] = pending.get("compiled_at")
            pending["test_count"] = int(pending.get("test_count") or 0) + 1
            if _compiled_execution_routines(pending_spec):
                _, _, routine_complete = _routine_preview_evidence(
                    pending,
                    spec=pending_spec,
                )
                if not routine_complete:
                    pending.pop("last_tested_at", None)
                    pending.pop("last_tested_compiled_at", None)
                    pending["status"] = "partially_tested"
            else:
                pending["last_tested_at"] = now_iso
                pending["last_tested_compiled_at"] = pending.get("compiled_at")
                pending["status"] = "tested"
            builder["pending_revision"] = pending
            settings_value["employee_builder"] = builder
            config.settings = settings_value
            db.commit()
            return {
                "agent_id": agent.id,
                "lifecycle": "live",
                "test_target": "pending_revision",
                "message": response.text,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "total_tokens": response.total_tokens,
                },
                "free_tests_remaining": None,
                "tools_used": False,
                "channels_used": False,
            }

        has_entitlement = _has_ai_agents_entitlement(db, current_user.company_id)
        is_self_service = str(company.onboarding_source or "managed") == "self_service"
        free_tests_remaining = None
        if has_entitlement or (is_self_service and not settings.SELF_SERVICE_REQUIRE_SUBSCRIPTION):
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
        builder["chat_tested_at"] = now_iso
        builder["chat_tested_compiled_at"] = builder.get("compiled_at")
        builder["test_count"] = int(builder.get("test_count") or 0) + 1
        current_spec = (
            builder.get("compiled_spec")
            if isinstance(builder.get("compiled_spec"), dict)
            else None
        )
        if _compiled_execution_routines(current_spec):
            _, _, routine_complete = _routine_preview_evidence(
                builder,
                spec=current_spec or {},
            )
            if not routine_complete:
                builder.pop("last_tested_at", None)
                builder.pop("last_tested_compiled_at", None)
        else:
            builder["last_tested_at"] = now_iso
            builder["last_tested_compiled_at"] = builder.get("compiled_at")
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


@router.post("/{agent_id}/build-pending-revision")
def build_pending_live_revision(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    """Build a staged live revision without changing the live employee runtime."""
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Live revisions are available only for Self-Service employees")
        if not _has_ai_agents_entitlement(db, company.id):
            raise HTTPException(
                403,
                detail={
                    "message": "Subscribe to build this staged revision",
                    "subscription_required": True,
                },
            )
        limits_service.check_token_limit(db, company.id)
        agent = db.query(AIAgent).filter(
            AIAgent.id == int(agent_id),
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        if not agent.enabled:
            raise HTTPException(409, "This employee is not live")

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = deepcopy(builder.get("pending_revision"))
        if not isinstance(pending, dict):
            raise HTTPException(404, "No staged revision is available")
        job_brief = str(pending.get("source_description") or "").strip()
        if not job_brief:
            raise HTTPException(409, "Pending revision Job Brief is missing")

        previous_spec = (
            pending.get("compiled_spec")
            if isinstance(pending.get("compiled_spec"), dict)
            else (
                builder.get("compiled_spec")
                if isinstance(builder.get("compiled_spec"), dict)
                else None
            )
        )
        staged = _compile_staged_employee_spec(
            db,
            company_id=company.id,
            agent=agent,
            job_brief=job_brief,
            requested_channels=list(pending.get("requested_channels") or []),
            previous_spec=previous_spec,
        )
        pending.update(staged)
        pending["status"] = "built"
        pending["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _invalidate_preview_evidence(pending)
        builder["pending_revision"] = pending
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.commit()
        return {
            "status": "pending_revision_built",
            "agent_id": agent.id,
            "compiled_at": pending.get("compiled_at"),
            "live_employee_unchanged": True,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/discard-pending-revision")
def discard_pending_live_revision(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(
                409,
                "Live revisions are available only for Self-Service employees",
            )
        agent = db.query(AIAgent).filter(
            AIAgent.id == int(agent_id),
            AIAgent.company_id == company.id,
        ).first()
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)
        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        if not isinstance(builder.get("pending_revision"), dict):
            raise HTTPException(404, "No staged revision is available")
        builder.pop("pending_revision", None)
        settings_value["employee_builder"] = builder
        config.settings = settings_value
        db.commit()
        return {
            "status": "pending_revision_discarded",
            "agent_id": agent.id,
            "live_employee_unchanged": True,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/apply-pending-revision")
def apply_pending_live_revision(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    """Atomically replace a live Self-Service employee with its tested staged revision."""
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(
                409,
                "Live revisions are available only for Self-Service employees",
            )
        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        if not agent.enabled:
            raise HTTPException(409, "This employee is not live")

        config = _employee_config_or_404(db, agent)
        db.refresh(config, with_for_update=True)
        db.refresh(agent, with_for_update=True)

        settings_value = dict(config.settings or {})
        builder = dict(settings_value.get("employee_builder") or {})
        pending = deepcopy(builder.get("pending_revision"))
        if not isinstance(pending, dict):
            raise HTTPException(404, "No staged revision is available")
        pending_spec = pending.get("compiled_spec")
        if not isinstance(pending_spec, dict):
            raise HTTPException(409, "Build the staged revision before applying it")
        compiled_at = str(pending.get("compiled_at") or "").strip()
        if (
            not compiled_at
            or str(pending.get("last_tested_compiled_at") or "").strip()
            != compiled_at
        ):
            raise HTTPException(409, "Test the staged revision before applying it")
        if str(pending.get("base_compiled_at") or "") != str(
            builder.get("compiled_at") or ""
        ):
            raise HTTPException(
                409,
                "The live employee changed after this revision was staged. Rebuild the revision from the current live version.",
            )

        previous_capabilities = dict(config.capabilities or {})
        next_capabilities = {
            str(key): bool(value)
            for key, value in dict(pending.get("capabilities") or {}).items()
            if str(key).strip() and bool(value)
        }
        desired_channels = set(
            communication_channels(pending.get("requested_channels") or [])
        )

        builder = _snapshot_builder_version(
            builder,
            reason="before_live_revision",
            capabilities=previous_capabilities,
        )
        _clear_generated_self_service_build(
            db,
            company_id=company.id,
            agent_id=agent.id,
        )

        for key in (
            "source_description",
            "job_brief",
            "audience",
            "requested_channels",
            "permissions",
            "setup_answers",
            "compiled_at",
            "compiler_provider",
            "compiler_model",
            "last_tested_at",
            "last_tested_compiled_at",
        ):
            if key in pending:
                builder[key] = deepcopy(pending[key])
        builder["compiled_spec"] = deepcopy(pending_spec)
        builder.pop("pending_revision", None)

        _reconcile_builder_runtime_tools(
            db,
            agent_id=agent.id,
            previous_capabilities=previous_capabilities,
            next_capabilities=tuple(next_capabilities.keys()),
        )
        config.capabilities = next_capabilities
        agent.description = str(builder.get("source_description") or "").strip()

        settings_value["employee_builder"] = builder
        compiled_spec = _store_provisioned_spec(
            db,
            company_id=company.id,
            agent=agent,
            config=config,
            settings=settings_value,
            builder=builder,
            spec=deepcopy(pending_spec),
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
            profile.instructions = agent.description
            profile.business_type = (
                "personal" if builder.get("audience") == "personal" else None
            )

        channel_rows = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == company.id,
                AgentChannel.agent_id == agent.id,
            )
            .with_for_update()
            .all()
        )
        channels_by_type = {
            str(row.channel_type or "").strip().lower(): row
            for row in channel_rows
        }
        desired_external = {item for item in desired_channels if item != "xvond"}

        for channel_type, channel in channels_by_type.items():
            if (
                channel.enabled
                and communication_channels([channel_type])
                and channel_type not in desired_external
            ):
                channel.enabled = False

        missing_rows = [
            item for item in sorted(desired_external)
            if item not in channels_by_type
        ]
        if missing_rows:
            raise HTTPException(
                409,
                detail={
                    "message": "The staged revision needs channel setup before it can replace the live employee",
                    "blockers": [
                        f"Configure {item} before applying this revision"
                        for item in missing_rows
                    ],
                },
            )

        for channel_type in sorted(desired_external):
            channel = channels_by_type[channel_type]
            blockers = self_service_channel_activation_blockers(
                db,
                company=company,
                agent=agent,
                channel=channel,
            )
            if blockers:
                raise HTTPException(
                    409,
                    detail={
                        "message": f"{channel_type} is not ready for the staged revision",
                        "blockers": blockers,
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
                    "message": "The staged revision is not ready to replace the live employee",
                    "blockers": live["blockers"],
                },
            )

        db.commit()
        return {
            **live,
            "status": "revision_applied",
            "agent_id": agent.id,
            "compiled_spec": self_service_spec_view(compiled_spec),
            "live_employee_replaced_atomically": True,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _employee_routine_rows(db, *, company_id: int, agent_id: int) -> list[AutomationWorkflow]:
    rows = (
        db.query(AutomationWorkflow)
        .filter(AutomationWorkflow.company_id == int(company_id))
        .order_by(AutomationWorkflow.id.asc())
        .all()
    )
    result: list[AutomationWorkflow] = []
    for row in rows:
        config = row.trigger_config if isinstance(row.trigger_config, dict) else {}
        if (
            config.get("_xvond_source") == "self_service_employee"
            and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
            and config.get("_xvond_graph_trigger") is True
        ):
            result.append(row)
    return result


def _runtime_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + (
        "" if value.tzinfo is not None else "Z"
    )


def _run_duration_ms(run: AutomationRun | None) -> int | None:
    if run is None or run.created_at is None:
        return None
    end = run.finished_at
    if end is None and run.status in {"queued", "running"}:
        end = datetime.utcnow()
    if end is None:
        return None
    start = run.created_at
    if start.tzinfo is not None and end.tzinfo is None:
        start = start.replace(tzinfo=None)
    elif start.tzinfo is None and end.tzinfo is not None:
        end = end.replace(tzinfo=None)
    return max(0, int((end - start).total_seconds() * 1000))


def _run_failure_detail(run: AutomationRun | None) -> dict | None:
    if run is None or run.status not in {"failed", "waiting_retry"}:
        return None

    output = run.output_data if isinstance(run.output_data, dict) else {}
    trace = output.get("trace") if isinstance(output.get("trace"), dict) else {}
    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    failed_span = next(
        (
            item
            for item in reversed(spans)
            if isinstance(item, dict)
            and str(item.get("status") or "").lower() == "failed"
        ),
        None,
    )
    if isinstance(failed_span, dict):
        error = str(failed_span.get("error") or run.error_message or "")
        node_id = failed_span.get("node_id")
        if not node_id and error:
            match = re.search(
                r"Execution graph (?:(?:foreach|repeat) )?node ([A-Za-z0-9_.:-]+)",
                error,
            )
            if match:
                node_id = match.group(1)
        return {
            "step_index": failed_span.get("step_index"),
            "step_type": failed_span.get("step_type"),
            "node_id": node_id,
            "phase": failed_span.get("phase"),
            "error": error or None,
            "duration_ms": failed_span.get("duration_ms"),
        }

    error = str(run.error_message or "")
    match = re.search(
        r"Execution graph (?:(?:foreach|repeat) )?node ([A-Za-z0-9_.:-]+)",
        error,
    )
    return {
        "step_index": None,
        "step_type": None,
        "node_id": match.group(1) if match else None,
        "phase": None,
        "error": error or None,
        "duration_ms": None,
    }


def _run_retry_state(run: AutomationRun | None) -> dict | None:
    if run is None or run.status != "failed":
        return None

    output = run.output_data if isinstance(run.output_data, dict) else {}
    checkpoint = (
        output.get("retry_checkpoint")
        if isinstance(output.get("retry_checkpoint"), dict)
        else None
    )
    if checkpoint is None:
        return {
            "safe": False,
            "run_id": run.id,
            "reason": (
                "This run has no durable node checkpoint. Start a new run instead "
                "of replaying uncertain completed work."
            ),
            "node_id": None,
            "node_type": None,
            "node_scope": None,
            "attempts": int(output.get("retry_attempts") or 0),
        }

    safe = bool(checkpoint.get("safe"))
    reason = str(checkpoint.get("reason") or "").strip()
    if safe and not reason:
        reason = (
            "Retry will resume from the last durable node checkpoint without "
            "replaying completed nodes."
        )
    elif not safe and not reason:
        reason = "Xvond cannot prove that replaying the failed node is safe."

    return {
        "safe": safe,
        "run_id": run.id,
        "reason": reason,
        "node_id": checkpoint.get("failed_node_id"),
        "node_type": checkpoint.get("failed_node_type"),
        "node_scope": checkpoint.get("failed_node_scope"),
        "attempts": int(output.get("retry_attempts") or 0),
    }


def _routine_health_summary(recent_runs: list[AutomationRun]) -> dict:
    runs = list(recent_runs or [])
    successes = [run for run in runs if run.status == "success"]
    failures = [run for run in runs if run.status == "failed"]
    rejected = [run for run in runs if run.status == "rejected"]
    terminal_count = len(successes) + len(failures) + len(rejected)

    consecutive_failures = 0
    for run in runs:
        if run.status == "failed":
            consecutive_failures += 1
            continue
        if run.status in {"success", "rejected"}:
            break

    success_rate = (
        round((len(successes) / terminal_count) * 100, 1)
        if terminal_count
        else None
    )
    last_success = successes[0] if successes else None
    last_failure = failures[0] if failures else None

    return {
        "window_size": len(runs),
        "success_count": len(successes),
        "failure_count": len(failures),
        "rejected_count": len(rejected),
        "success_rate_percent": success_rate,
        "consecutive_failures": consecutive_failures,
        "last_success_at": _runtime_datetime(
            last_success.finished_at or last_success.created_at
        ) if last_success is not None else None,
        "last_failure_at": _runtime_datetime(
            last_failure.finished_at or last_failure.created_at
        ) if last_failure is not None else None,
    }


def _routine_operational_state(
    *,
    workflow: AutomationWorkflow,
    recent_runs: list[AutomationRun],
    employee_enabled: bool,
) -> dict:
    latest_run = recent_runs[0] if recent_runs else None
    trigger_config = (
        workflow.trigger_config
        if isinstance(workflow.trigger_config, dict)
        else {}
    )

    if not employee_enabled:
        state = "employee_paused"
    elif not workflow.enabled:
        state = "paused"
    elif latest_run is None:
        state = "never_run"
    elif latest_run.status in {"queued", "running"}:
        state = "running"
    elif latest_run.status == "waiting_time":
        state = "waiting_time"
    elif latest_run.status == "waiting_event":
        state = "waiting_event"
    elif latest_run.status == "waiting_approval":
        state = "waiting_approval"
    elif latest_run.status == "waiting_retry":
        state = "recovering"
    elif latest_run.status == "failed":
        state = "needs_attention"
    elif latest_run.status == "success":
        state = "healthy"
    elif latest_run.status == "rejected":
        state = "rejected"
    else:
        state = str(latest_run.status or "unknown")

    next_scheduled_at = None
    schedule = trigger_config.get("schedule")
    if (
        workflow.trigger_type == "schedule"
        and isinstance(schedule, dict)
        and workflow.enabled
        and employee_enabled
    ):
        schedule_start = workflow.created_at
        resumed_at = str(trigger_config.get("_xvond_resumed_at") or "").strip()
        if resumed_at:
            try:
                schedule_start = datetime.fromisoformat(
                    resumed_at.replace("Z", "+00:00")
                )
            except ValueError:
                schedule_start = workflow.created_at
        try:
            next_slot = next_schedule_slot(
                schedule,
                after=datetime.utcnow(),
                created_at=schedule_start,
            )
            next_scheduled_at = _runtime_datetime(next_slot)
        except (ScheduleConfigError, ValueError):
            next_scheduled_at = None

    waiting = None
    if latest_run is not None and latest_run.status == "waiting_time":
        waiting = {
            "type": "time",
            "resume_at": _runtime_datetime(latest_run.resume_at),
        }
    elif latest_run is not None and latest_run.status == "waiting_event":
        waiting = {
            "type": "event",
            "event_name": latest_run.resume_event_name,
        }
    elif latest_run is not None and latest_run.status == "waiting_approval":
        approval = (
            (latest_run.output_data or {}).get("approval")
            if isinstance(latest_run.output_data, dict)
            else None
        )
        waiting = {
            "type": "approval",
            "request_id": (
                approval.get("request_id")
                if isinstance(approval, dict)
                else None
            ),
            "action_type": (
                approval.get("action_type")
                if isinstance(approval, dict)
                else None
            ),
        }
    elif latest_run is not None and latest_run.status == "waiting_retry":
        retry_meta = (
            (latest_run.output_data or {}).get("retry")
            if isinstance(latest_run.output_data, dict)
            else None
        )
        waiting = {
            "type": "retry",
            "resume_at": _runtime_datetime(latest_run.resume_at),
            "attempt": (
                retry_meta.get("attempt")
                if isinstance(retry_meta, dict)
                else None
            ),
            "max_attempts": (
                retry_meta.get("max_attempts")
                if isinstance(retry_meta, dict)
                else None
            ),
            "last_error": latest_run.error_message,
        }

    failure = _run_failure_detail(latest_run)
    return {
        "operational_state": state,
        "next_scheduled_at": next_scheduled_at,
        "waiting": waiting,
        "health": _routine_health_summary(recent_runs),
        "failure": failure,
        "retry": _run_retry_state(latest_run),
        "last_run": (
            {
                "id": latest_run.id,
                "status": latest_run.status,
                "created_at": _runtime_datetime(latest_run.created_at),
                "finished_at": _runtime_datetime(latest_run.finished_at),
                "duration_ms": _run_duration_ms(latest_run),
                "error_message": latest_run.error_message,
            }
            if latest_run is not None
            else None
        ),
    }


def _workflow_routine_id(workflow: AutomationWorkflow) -> str:
    config = workflow.trigger_config if isinstance(workflow.trigger_config, dict) else {}
    return normalize_requirement_key(config.get("_xvond_routine_id") or "primary") or "primary"


def _sync_routine_delivery_state(
    config: AgentConfig,
    *,
    workflow: AutomationWorkflow,
    routine_id: str,
    enabled: bool,
) -> None:
    settings_value = deepcopy(dict(config.settings or {}))
    builder = dict(settings_value.get("employee_builder") or {})
    compiled_spec = (
        deepcopy(builder.get("compiled_spec"))
        if isinstance(builder.get("compiled_spec"), dict)
        else None
    )
    if not isinstance(compiled_spec, dict):
        return

    delivery = (
        deepcopy(compiled_spec.get("delivery"))
        if isinstance(compiled_spec.get("delivery"), dict)
        else {}
    )
    status = "ready" if enabled else "disabled"

    graph_triggers = [
        dict(item)
        for item in (delivery.get("graph_triggers") or [])
        if isinstance(item, dict)
    ]
    for item in graph_triggers:
        stored_id = normalize_requirement_key(item.get("routine_id") or "primary") or "primary"
        if stored_id == routine_id or int(item.get("workflow_id") or 0) == int(workflow.id):
            item["status"] = status
    if graph_triggers:
        delivery["graph_triggers"] = graph_triggers

    graph_trigger = delivery.get("graph_trigger")
    if isinstance(graph_trigger, dict):
        primary = dict(graph_trigger)
        stored_id = normalize_requirement_key(primary.get("routine_id") or "primary") or "primary"
        if stored_id == routine_id or int(primary.get("workflow_id") or 0) == int(workflow.id):
            primary["status"] = status
        delivery["graph_trigger"] = primary

    compiled_spec["delivery"] = delivery
    builder["compiled_spec"] = compiled_spec
    builder["delivery"] = deepcopy(delivery)
    settings_value["employee_builder"] = builder
    config.settings = settings_value


@router.get("/{agent_id}/routines")
def customer_employee_routines(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Routine controls are available only for Self-Service employees")

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

        workflow_rows = _employee_routine_rows(
            db,
            company_id=company_id,
            agent_id=agent.id,
        )
        recent_runs_by_workflow: dict[int, list[AutomationRun]] = {}
        for workflow in workflow_rows:
            recent_runs_by_workflow[workflow.id] = (
                db.query(AutomationRun)
                .filter(
                    AutomationRun.company_id == company_id,
                    AutomationRun.workflow_id == workflow.id,
                )
                .order_by(AutomationRun.id.desc())
                .limit(20)
                .all()
            )

        routines = []
        for workflow in workflow_rows:
            trigger_config = (
                workflow.trigger_config
                if isinstance(workflow.trigger_config, dict)
                else {}
            )
            operational = _routine_operational_state(
                workflow=workflow,
                recent_runs=recent_runs_by_workflow.get(workflow.id) or [],
                employee_enabled=bool(agent.enabled),
            )
            routines.append(
                {
                    "routine_id": _workflow_routine_id(workflow),
                    "routine_name": trigger_config.get("_xvond_routine_name") or workflow.name,
                    "workflow_id": workflow.id,
                    "trigger_type": workflow.trigger_type,
                    "enabled": bool(workflow.enabled),
                    "paused_at": trigger_config.get("_xvond_paused_at"),
                    "resumed_at": trigger_config.get("_xvond_resumed_at"),
                    "schedule": (
                        deepcopy(trigger_config.get("schedule"))
                        if isinstance(trigger_config.get("schedule"), dict)
                        else None
                    ),
                    **operational,
                }
            )

        return {
            "agent_id": agent.id,
            "employee_enabled": bool(agent.enabled),
            "routines": routines,
        }
    finally:
        db.close()


@router.put("/{agent_id}/routines/{routine_id}")
def customer_employee_set_routine_state(
    agent_id: int,
    routine_id: str,
    data: EmployeeBuilderRoutineStateRequest,
    current_user: User = Depends(require_customer_manager),
):
    normalized_routine = normalize_requirement_key(routine_id)
    if not normalized_routine:
        raise HTTPException(400, "Routine id is invalid")

    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Routine controls are available only for Self-Service employees")

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
        db.refresh(config, with_for_update=True)

        workflow = next(
            (
                row
                for row in _employee_routine_rows(
                    db,
                    company_id=company_id,
                    agent_id=agent.id,
                )
                if _workflow_routine_id(row) == normalized_routine
            ),
            None,
        )
        if workflow is None:
            raise HTTPException(404, "Employee routine not found")

        trigger_config = dict(workflow.trigger_config or {})
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        workflow.enabled = bool(data.enabled)
        if data.enabled:
            trigger_config["_xvond_resumed_at"] = now_iso
            trigger_config.pop("_xvond_paused_at", None)
        else:
            trigger_config["_xvond_paused_at"] = now_iso
        workflow.trigger_config = trigger_config

        _sync_routine_delivery_state(
            config,
            workflow=workflow,
            routine_id=normalized_routine,
            enabled=bool(data.enabled),
        )
        db.commit()

        return {
            "status": "resumed" if data.enabled else "paused",
            "agent_id": agent.id,
            "routine_id": normalized_routine,
            "routine_name": trigger_config.get("_xvond_routine_name") or workflow.name,
            "workflow_id": workflow.id,
            "trigger_type": workflow.trigger_type,
            "enabled": bool(workflow.enabled),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/{agent_id}/routines/{routine_id}/retry")
def customer_employee_retry_routine(
    agent_id: int,
    routine_id: str,
    data: EmployeeBuilderRoutineRetryRequest,
    current_user: User = Depends(require_customer_manager),
):
    normalized_routine = normalize_requirement_key(routine_id)
    if not normalized_routine:
        raise HTTPException(400, "Routine id is invalid")

    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Routine retry is available only for Self-Service employees")

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
            raise HTTPException(409, "Launch this employee before retrying its routine")

        workflow = next(
            (
                row
                for row in _employee_routine_rows(
                    db,
                    company_id=company_id,
                    agent_id=agent.id,
                )
                if _workflow_routine_id(row) == normalized_routine
            ),
            None,
        )
        if workflow is None:
            raise HTTPException(404, "Employee routine not found")
        db.refresh(workflow, with_for_update=True)
        if not workflow.enabled:
            raise HTTPException(409, "Resume this routine before retrying it")

        latest_run = (
            db.query(AutomationRun)
            .filter(
                AutomationRun.company_id == company_id,
                AutomationRun.workflow_id == workflow.id,
            )
            .order_by(AutomationRun.id.desc())
            .with_for_update()
            .first()
        )
        if latest_run is None:
            raise HTTPException(409, "This routine has no run to retry")
        if int(latest_run.id) != int(data.run_id):
            raise HTTPException(
                409,
                detail={
                    "message": "A newer routine run exists. Refresh before retrying.",
                    "latest_run_id": latest_run.id,
                },
            )
        if latest_run.status != "failed":
            raise HTTPException(409, "Only a failed routine run can be retried")

        retry_state = _run_retry_state(latest_run)
        if not retry_state or not retry_state.get("safe"):
            raise HTTPException(
                409,
                detail={
                    "message": (
                        (retry_state or {}).get("reason")
                        or "This failed run cannot be retried safely"
                    ),
                    "retry": retry_state,
                },
            )

        try:
            retried = automation_runtime.retry_failed(
                db,
                company_id=company_id,
                workflow=workflow,
                run=latest_run,
            )
        except Exception as exc:
            db.rollback()
            current = db.get(AutomationRun, int(data.run_id))
            if current is not None and current.status == "failed":
                raise HTTPException(
                    409,
                    detail={
                        "message": current.error_message or str(exc),
                        "run_id": current.id,
                        "retry": _run_retry_state(current),
                    },
                ) from exc
            if isinstance(exc, ValueError):
                raise HTTPException(409, str(exc)) from exc
            raise

        return {
            "status": retried.status,
            "agent_id": agent.id,
            "routine_id": normalized_routine,
            "routine_name": (
                (workflow.trigger_config or {}).get("_xvond_routine_name")
                or workflow.name
            ),
            "workflow_id": workflow.id,
            "run_id": retried.id,
            "output_data": retried.output_data,
            "error_message": retried.error_message,
            "finished_at": retried.finished_at,
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
    routine_id: str | None = None,
):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        if company_id is None:
            raise HTTPException(403, "Customer company required")
        company = _company_or_404(db, company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
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
        graph_triggers = (
            [
                item
                for item in (delivery.get("graph_triggers") or [])
                if isinstance(item, dict)
            ]
            if isinstance(delivery, dict)
            and isinstance(delivery.get("graph_triggers"), list)
            else []
        )
        if not graph_triggers and isinstance(delivery, dict):
            legacy = delivery.get("graph_trigger")
            if isinstance(legacy, dict):
                graph_triggers = [legacy]

        webhook_triggers = [
            item for item in graph_triggers
            if item.get("trigger_type") == "webhook"
        ]
        requested_routine = normalize_requirement_key(routine_id) if routine_id else ""
        if requested_routine:
            webhook_triggers = [
                item
                for item in webhook_triggers
                if normalize_requirement_key(item.get("routine_id") or "primary")
                == requested_routine
            ]

        if not webhook_triggers:
            raise HTTPException(404, "This employee does not use the requested webhook routine")
        if not requested_routine and len(webhook_triggers) > 1:
            raise HTTPException(
                409,
                detail={
                    "message": "This employee has multiple webhook routines; choose routine_id.",
                    "routines": [
                        {
                            "routine_id": item.get("routine_id") or "primary",
                            "routine_name": item.get("routine_name") or "Webhook routine",
                        }
                        for item in webhook_triggers
                    ],
                },
            )

        graph_trigger = webhook_triggers[0]
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
            "routine_id": graph_trigger.get("routine_id") or "primary",
            "routine_name": graph_trigger.get("routine_name") or workflow.name,
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
        if not _agent_id_is_self_service(db, company, agent_id):
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
        workflow_routines = {}
        for workflow in workflows:
            config = workflow.trigger_config if isinstance(workflow.trigger_config, dict) else {}
            if (
                config.get("_xvond_source") == "self_service_employee"
                and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
            ):
                workflow_ids.append(workflow.id)
                workflow_names[workflow.id] = workflow.name
                workflow_routines[workflow.id] = {
                    "routine_id": config.get("_xvond_routine_id") or (
                        "primary" if config.get("_xvond_graph_trigger") is True else None
                    ),
                    "routine_name": config.get("_xvond_routine_name"),
                }

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
                    "routine_id": (
                        workflow_routines.get(run.workflow_id) or {}
                    ).get("routine_id"),
                    "routine_name": (
                        workflow_routines.get(run.workflow_id) or {}
                    ).get("routine_name"),
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
        if not _agent_id_is_self_service(db, company, agent_id):
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

        requested_routine = normalize_requirement_key(data.routine_id) if data.routine_id else ""
        candidates: list[AutomationWorkflow] = []
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
            stored_routine = normalize_requirement_key(
                config.get("_xvond_routine_id") or "primary"
            )
            if (
                config.get("_xvond_source") == "self_service_employee"
                and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
                and config.get("_xvond_graph_trigger") is True
                and (not requested_routine or stored_routine == requested_routine)
            ):
                candidates.append(row)

        if not candidates:
            raise HTTPException(409, "This employee does not have the requested ready manual routine")
        if not requested_routine and len(candidates) > 1:
            raise HTTPException(
                409,
                detail={
                    "message": "This employee has multiple manual routines; choose routine_id.",
                    "routines": [
                        {
                            "routine_id": (
                                (row.trigger_config or {}).get("_xvond_routine_id")
                                or "primary"
                            ),
                            "routine_name": (
                                (row.trigger_config or {}).get("_xvond_routine_name")
                                or row.name
                            ),
                        }
                        for row in candidates
                    ],
                },
            )
        workflow = candidates[0]
        workflow_config = (
            workflow.trigger_config
            if isinstance(workflow.trigger_config, dict)
            else {}
        )

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
            "routine_id": workflow_config.get("_xvond_routine_id") or "primary",
            "routine_name": workflow_config.get("_xvond_routine_name") or workflow.name,
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

@router.get("/{agent_id}/files")
def list_employee_file_assets(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Employee file assets are available through the Self-Service builder")
        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")
        rows = (
            db.query(EmployeeFileAsset)
            .filter(
                EmployeeFileAsset.company_id == company.id,
                EmployeeFileAsset.agent_id == agent.id,
                EmployeeFileAsset.enabled.is_(True),
            )
            .order_by(EmployeeFileAsset.id.desc())
            .all()
        )
        return {
            "agent_id": agent.id,
            "files": [
                {
                    "id": item.id,
                    "filename": item.filename,
                    "content_type": item.content_type,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "created_at": item.created_at,
                }
                for item in rows
            ],
        }
    finally:
        db.close()


@router.post("/{agent_id}/files")
async def upload_employee_file_asset(
    agent_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Employee file assets are available through the Self-Service builder")
        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == int(agent_id),
                AIAgent.company_id == company.id,
            )
            .first()
        )
        if agent is None:
            raise HTTPException(404, "AI employee not found")

        raw = await file.read(MAX_EMPLOYEE_FILE_BYTES + 1)
        if not raw:
            raise HTTPException(400, "File is empty")
        if len(raw) > MAX_EMPLOYEE_FILE_BYTES:
            raise HTTPException(413, "File is larger than 15 MB")

        filename = _safe_asset_filename(file.filename)
        content_type = str(file.content_type or "application/octet-stream").strip().lower()
        if (
            len(content_type) > 120
            or not re.fullmatch(
                r"[a-z0-9!#&^_.+-]+/[a-z0-9!#&^_.+-]+",
                content_type,
            )
        ):
            content_type = "application/octet-stream"
        digest = hashlib.sha256(raw).hexdigest()

        existing = (
            db.query(EmployeeFileAsset)
            .filter(
                EmployeeFileAsset.company_id == company.id,
                EmployeeFileAsset.agent_id == agent.id,
                EmployeeFileAsset.sha256 == digest,
                EmployeeFileAsset.enabled.is_(True),
            )
            .first()
        )
        if existing is not None:
            return {
                "status": "existing",
                "id": existing.id,
                "filename": existing.filename,
                "content_type": existing.content_type,
                "size_bytes": existing.size_bytes,
                "sha256": existing.sha256,
            }

        asset = EmployeeFileAsset(
            company_id=company.id,
            agent_id=agent.id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(raw),
            sha256=digest,
            content=raw,
            enabled=True,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return {
            "status": "uploaded",
            "id": asset.id,
            "filename": asset.filename,
            "content_type": asset.content_type,
            "size_bytes": asset.size_bytes,
            "sha256": asset.sha256,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        await file.close()
        db.close()


@router.delete("/{agent_id}/files/{asset_id}")
def delete_employee_file_asset(
    agent_id: int,
    asset_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company = _company_or_404(db, current_user.company_id)
        if not _agent_id_is_self_service(db, company, agent_id):
            raise HTTPException(409, "Employee file assets are available through the Self-Service builder")
        asset = (
            db.query(EmployeeFileAsset)
            .filter(
                EmployeeFileAsset.id == int(asset_id),
                EmployeeFileAsset.company_id == company.id,
                EmployeeFileAsset.agent_id == int(agent_id),
                EmployeeFileAsset.enabled.is_(True),
            )
            .first()
        )
        if asset is None:
            raise HTTPException(404, "Employee file asset not found")
        asset.enabled = False
        db.commit()
        return {"status": "deleted", "id": asset.id}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()
