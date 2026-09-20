from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.config.settings import settings
from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.core.config_secrets import reveal_config
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent.employee_compiler import (
    is_sensitive_requirement_key,
    normalize_requirement_key,
)
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_ADAPTER_REQUIRED,
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_INTERNAL,
    CHANNEL_SETUP_MANAGED,
    CHANNEL_SETUP_SELF_SERVICE,
    N8N_CHANNEL_ADAPTER,
    canonical_channel_type,
    customer_channel_types,
    get_channel_capability,
    live_managed_channel_types,
    live_self_service_channel_types,
    packaged_managed_channel_types,
    validate_channel_config,
)
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state
from backend.app.modules.integrations.catalog import integration_validation_ready
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument


SELF_SERVICE_SOURCE = "self_service"

# Communication surfaces are defined once in the channel delivery registry.
# Publishing/actions (for example Instagram publishing or send-email actions)
# remain integrations; Instagram DM and Email can also be selected as employee
# communication surfaces and follow the channel delivery contract below.
COMMUNICATION_CHANNELS = frozenset(customer_channel_types())
EXTERNAL_COMMUNICATION_CHANNELS = COMMUNICATION_CHANNELS - {"xvond"}
SELF_SERVICE_LIVE_EXTERNAL_CHANNELS = (
    live_self_service_channel_types() - {"xvond"}
)
MANAGED_LIVE_EXTERNAL_CHANNELS = live_managed_channel_types()
PACKAGED_MANAGED_EXTERNAL_CHANNELS = packaged_managed_channel_types()


def is_self_service_company(company: Company | None) -> bool:
    return bool(
        company is not None
        and str(company.onboarding_source or "").strip().lower() == SELF_SERVICE_SOURCE
    )


def communication_channels(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        key = canonical_channel_type(item)
        if key in COMMUNICATION_CHANNELS and key not in result:
            result.append(key)
    return result


def self_service_channel_slots(builder: dict | None) -> list[str]:
    """Return only communication surfaces required by the current Self-Service job.

    This is intentionally a presentation/provisioning view: it combines channels
    explicitly requested by the Job Brief with channel requirements emitted by
    the compiled specification. Stale configured/active channels are not added,
    so customer setup UI follows the current employee contract rather than old
    connection state.
    """

    builder = dict(builder or {})
    slots = communication_channels(builder.get("requested_channels") or [])
    spec = builder.get("compiled_spec")
    if isinstance(spec, dict):
        for item in _requirement_channel_keys(spec):
            if item not in slots:
                slots.append(item)
    return slots


def assert_self_service_channel_selected(
    db,
    *,
    company: Company,
    agent: AIAgent,
    channel_type: str,
) -> None:
    """Fail closed when customer setup targets a channel outside the current job."""

    if not is_self_service_company(company):
        return

    config = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id == agent.id)
        .first()
    )
    builder = (
        dict(config.settings or {}).get("employee_builder")
        if config is not None
        else None
    )
    selected = self_service_channel_slots(builder)
    key = canonical_channel_type(channel_type)
    if key not in selected:
        raise HTTPException(
            409,
            f"{key or 'channel'} is not selected by this employee's current Job Brief",
        )


def _requirement_channel_keys(spec: dict) -> list[str]:
    result: list[str] = []
    for item in spec.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip().lower() != "channel":
            continue
        key = canonical_channel_type(item.get("key"))
        if key == "xvond_workspace":
            key = "xvond"
        if key in COMMUNICATION_CHANNELS and key not in result:
            result.append(key)
    return result


def self_service_connection_status(item: dict) -> str | None:
    status = str(item.get("status") or "").strip().lower()
    if status != "connection_required":
        return None
    kind = str(item.get("kind") or "").strip().lower()
    if kind != "channel":
        return "self_service_integration_available"

    key = canonical_channel_type(item.get("key"))
    if key == "xvond_workspace":
        key = "xvond"
    capability = get_channel_capability(key)
    if capability is None:
        return "xvond_adapter_required"
    if capability.get("setup_mode") == CHANNEL_SETUP_INTERNAL:
        return "internal_available"
    if (
        capability.get("runtime_state") == CHANNEL_RUNTIME_LIVE
        and capability.get("setup_mode") == CHANNEL_SETUP_SELF_SERVICE
    ):
        return "self_service_available"
    if (
        capability.get("runtime_state") == CHANNEL_RUNTIME_LIVE
        and capability.get("setup_mode") == CHANNEL_SETUP_MANAGED
    ):
        if capability.get("packaged_provider") is True:
            return "xvond_managed_available"
        return "xvond_custom_provider_setup"
    return "xvond_adapter_required"


def self_service_spec_view(spec: dict | None) -> dict | None:
    """Annotate a compiled spec for truthful Self-Service connection UX.

    This is view-layer metadata so cached compiler output does not need a paid
    recompilation when Xvond's connection surface changes.
    """
    if not isinstance(spec, dict):
        return None
    rendered = deepcopy(spec)
    for item in rendered.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        connection_status = self_service_connection_status(item)
        if connection_status:
            item["self_service_connection_status"] = connection_status
        if str(item.get("kind") or "").strip().lower() == "channel":
            channel_key = canonical_channel_type(item.get("key"))
            if channel_key == "xvond_workspace":
                channel_key = "xvond"
            capability = get_channel_capability(channel_key)
            if capability is not None:
                item["channel_delivery"] = {
                    "type": channel_key,
                    "name": capability.get("name"),
                    "setup_mode": capability.get("setup_mode"),
                    "runtime_state": capability.get("runtime_state"),
                    "runtime_adapter": (
                        "xvond_managed"
                        if capability.get("setup_mode") == CHANNEL_SETUP_MANAGED
                        else "xvond_native"
                    ),
                }
    return rendered


def interaction_mode(spec: dict | None, requested_channels: list[str] | tuple[str, ...]) -> str:
    spec = spec or {}
    requested = communication_channels(requested_channels)
    required = _requirement_channel_keys(spec)
    has_customer_channel = bool(requested or required)

    requirements = [
        item for item in (spec.get("requirements") or []) if isinstance(item, dict)
    ]
    has_automation = any(
        str(item.get("kind") or "").strip().lower() == "automation"
        or "scheduler" in (item.get("primitives") or [])
        for item in requirements
    )
    scope = str(spec.get("scope") or "").strip().lower()

    if has_customer_channel and (scope == "personal" or has_automation):
        return "hybrid"
    if has_customer_channel:
        return "customer_facing"
    if has_automation:
        return "background"
    if scope == "personal":
        return "personal"
    return "workspace"


def channel_limit_from_plan(plan) -> int | None:
    if plan is None:
        return None
    limits = dict(getattr(plan, "limits", None) or {})
    raw = limits.get("channels_per_employee", limits.get("channels"))
    if raw in (None, "", 0, "0"):
        return None
    try:
        value = int(Decimal(str(raw)))
    except Exception as exc:
        raise HTTPException(500, "AI Agents plan has an invalid channel limit") from exc
    if value < 0:
        raise HTTPException(500, "AI Agents plan has an invalid channel limit")
    return value


def subscription_snapshot(db, company_id: int) -> dict:
    try:
        subscription, plan = service_limits.entitlement(db, company_id, "ai_agents")
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        pending = (
            db.query(ServiceSubscription)
            .filter(
                ServiceSubscription.company_id == company_id,
                ServiceSubscription.service_code == "ai_agents",
            )
            .first()
        )
        pending_plan = db.get(ServicePlan, pending.plan_id) if pending is not None else None
        return {
            "active": False,
            "subscription": pending,
            "plan": pending_plan,
            "plan_name": pending_plan.name if pending_plan is not None else None,
            "plan_tier": pending_plan.tier if pending_plan is not None else None,
            "subscription_status": pending.status if pending is not None else None,
            "channel_limit": None,
        }

    return {
        "active": True,
        "subscription": subscription,
        "plan": plan,
        "plan_name": plan.name,
        "plan_tier": plan.tier,
        "subscription_status": subscription.status,
        "channel_limit": channel_limit_from_plan(plan),
    }


def enabled_channel_types(db, *, company_id: int, agent_id: int) -> list[str]:
    rows = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == company_id,
            AgentChannel.agent_id == agent_id,
            AgentChannel.enabled.is_(True),
        )
        .all()
    )
    result: list[str] = []
    for row in rows:
        key = canonical_channel_type(row.channel_type)
        if key in EXTERNAL_COMMUNICATION_CHANNELS and key not in result:
            result.append(key)
    return result


def configured_channel_types(db, *, company_id: int, agent_id: int) -> list[str]:
    """Communication channels with complete stored config, regardless of live state."""
    rows = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == company_id,
            AgentChannel.agent_id == agent_id,
        )
        .all()
    )
    result: list[str] = []
    for row in rows:
        key = canonical_channel_type(row.channel_type)
        if key in result:
            continue
        capability = get_channel_capability(key)
        if (
            capability is None
            or capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
            or key not in EXTERNAL_COMMUNICATION_CHANNELS
        ):
            continue

        config = reveal_config(row.config) or {}
        try:
            validate_channel_config(key, config)
        except ValueError:
            continue

        if capability.get("setup_mode") == CHANNEL_SETUP_MANAGED:
            if key == "voice":
                required = (
                    "vapi_assistant_id",
                    "vapi_phone_number_id",
                    "vapi_llm_credential_id",
                    "llm_api_key",
                )
                if str(config.get("provisioning_state") or "").strip().lower() != "connected":
                    continue
                if any(not str(config.get(item) or "").strip() for item in required):
                    continue
            elif capability.get("runtime_adapter") == N8N_CHANNEL_ADAPTER:
                if not n8n_gateway.configured():
                    continue
                if not str(config.get("connection_key") or "").strip():
                    continue
            else:
                continue

        result.append(key)
    return result


def self_service_channel_activation_blockers(
    db,
    *,
    company: Company,
    agent: AIAgent,
    channel: AgentChannel,
) -> list[str]:
    """Live activation checks for Self-Service communication channels only."""
    blockers: list[str] = []
    channel_type = canonical_channel_type(channel.channel_type)

    if channel.company_id != company.id or channel.agent_id != agent.id:
        return ["Communication channel ownership does not match this employee"]

    capability = get_channel_capability(channel_type)
    if capability is None:
        return [f"{channel_type or 'channel'} is not a registered communication channel"]
    if capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE:
        return [
            f"{capability.get('name') or channel_type}: Xvond runtime adapter is still required"
        ]
    if (
        capability.get("setup_mode") != CHANNEL_SETUP_MANAGED
        and channel_type not in SELF_SERVICE_LIVE_EXTERNAL_CHANNELS
    ):
        return [f"{channel_type or 'channel'} is not available for Self-Service launch"]

    if not company.active:
        blockers.append("Company must be active")
    if not agent.enabled:
        blockers.append("AI employee must be active")

    channels_module = (
        db.query(CompanyModule)
        .filter(
            CompanyModule.company_id == company.id,
            CompanyModule.module_name == "channels",
            CompanyModule.enabled.is_(True),
        )
        .first()
    )
    if channels_module is None:
        blockers.append("Channels module is not enabled")

    if settings.is_production:
        try:
            selections = runtime_selections(
                db,
                company.id,
                agent.provider,
                agent.model,
            )
        except Exception:
            selections = []
        if not any(item.provider != "mock" for item in selections):
            blockers.append("At least one real AI provider/model must be available")

    channel_config = reveal_config(channel.config) or {}
    try:
        validate_channel_config(channel_type, channel_config)
    except ValueError:
        blockers.append(
            f"{capability.get('name') or channel_type.title()} channel configuration is incomplete"
        )
        return blockers

    if channel_type == "whatsapp":
        connection = whatsapp_connection_state(
            channel_config,
            verify_remote=True,
        )
        if connection.get("connected") is not True:
            blockers.append(
                connection.get("connection_issue")
                or "WhatsApp must be connected and verified with Meta"
            )
    elif channel_type == "website":
        if not str(channel_config.get("widget_key") or "").strip():
            blockers.append("Website widget key is missing")
        if settings.is_production and not settings.PUBLIC_BASE_URL:
            blockers.append("Xvond public API URL is not configured")
    elif channel_type == "voice":
        required = (
            "vapi_assistant_id",
            "vapi_phone_number_id",
            "vapi_llm_credential_id",
            "llm_api_key",
        )
        if str(channel_config.get("provisioning_state") or "").strip().lower() != "connected":
            blockers.append("Voice: Xvond managed provisioning is not complete")
        elif any(not str(channel_config.get(key) or "").strip() for key in required):
            blockers.append("Voice: Vapi provisioning evidence is incomplete")
    elif capability.get("runtime_adapter") == N8N_CHANNEL_ADAPTER:
        if not n8n_gateway.configured():
            blockers.append("Xvond managed channel gateway is not configured")
        else:
            try:
                route_check = n8n_gateway.execute(
                    company_id=company.id,
                    agent_id=agent.id,
                    action="channel.check",
                    data={
                        "channel_id": channel.id,
                        "connection_key": str(channel_config.get("connection_key") or "").strip(),
                    },
                )
            except N8NGatewayError:
                blockers.append("Xvond managed channel route could not be verified")
            else:
                if (
                    route_check.get("success") is not True
                    or not isinstance(route_check.get("data"), dict)
                    or route_check["data"].get("configured") is not True
                ):
                    blockers.append("Xvond managed channel route is not configured")

    return blockers


def _setup_answer_complete(requirement: dict, answer: Any) -> bool:
    key = normalize_requirement_key(requirement.get("key"))
    if not key or is_sensitive_requirement_key(key):
        return False

    required_fields: list[str] = []
    for raw in requirement.get("customer_inputs") or []:
        field = normalize_requirement_key(raw)
        if not field or field in required_fields:
            continue
        if is_sensitive_requirement_key(field):
            return False
        required_fields.append(field)

    if required_fields:
        if not isinstance(answer, dict):
            return False
        return all(str(answer.get(field) or "").strip() for field in required_fields)

    return isinstance(answer, str) and bool(answer.strip())


def _resolved_customer_requirement_keys(
    db,
    *,
    company_id: int,
    agent_id: int,
) -> set[str]:
    resolved: set[str] = set()
    knowledge_query = (
        db.query(KnowledgeDocument)
        .join(AgentKnowledge, AgentKnowledge.document_id == KnowledgeDocument.id)
        .filter(
            AgentKnowledge.agent_id == agent_id,
            AgentKnowledge.enabled.is_(True),
            KnowledgeDocument.company_id == company_id,
            KnowledgeDocument.enabled.is_(True),
        )
    )
    if knowledge_query.count() > 0:
        resolved.add("knowledge")
    if knowledge_query.filter(KnowledgeDocument.source_type == "pdf").count() > 0:
        resolved.add("files")

    config = (
        db.query(AgentConfig)
        .filter(AgentConfig.agent_id == agent_id)
        .first()
    )
    if config is not None:
        builder = dict((config.settings or {}).get("employee_builder") or {})
        answers = builder.get("setup_answers") or {}
        spec = builder.get("compiled_spec")
        if isinstance(answers, dict) and isinstance(spec, dict):
            for requirement in spec.get("requirements") or []:
                if not isinstance(requirement, dict):
                    continue
                key = normalize_requirement_key(requirement.get("key"))
                if not key:
                    continue
                if str(requirement.get("status") or "").strip().lower() != "customer_input_required":
                    continue
                if _setup_answer_complete(requirement, answers.get(key)):
                    resolved.add(key)
    return resolved


def _self_service_provider_ready(db, *, company_id: int, agent: AIAgent) -> bool:
    if not settings.is_production:
        return True
    try:
        selections = runtime_selections(
            db,
            company_id,
            agent.provider,
            agent.model,
        )
    except Exception:
        return False
    return any(item.provider != "mock" for item in selections)


def _execution_blockers(
    spec: dict,
    *,
    resolved_channels: list[str],
    resolved_requirements: set[str] | None = None,
) -> list[str]:
    blockers: list[str] = []
    resolved = set(communication_channels(resolved_channels))
    resolved_customer_requirements = set(resolved_requirements or set())
    for item in spec.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "requirement").strip()
        kind = str(item.get("kind") or "").strip().lower()
        channel_key = "xvond" if key == "xvond_workspace" else canonical_channel_type(key)
        status = str(item.get("status") or "").strip().lower()
        execution_status = str(item.get("execution_status") or "").strip().lower()

        if kind == "channel" and channel_key in resolved:
            continue
        if status == "connection_required":
            if self_service_connection_status(item) == "xvond_adapter_required":
                blockers.append(f"{key}: Xvond connection adapter required")
            else:
                blockers.append(f"{key}: setup required")
            continue
        if status == "customer_input_required":
            if key.lower() in resolved_customer_requirements:
                continue
            blockers.append(f"{key}: setup required")
            continue
        if status == "setup_required":
            blockers.append(f"{key}: setup required")
            continue
        if status == "xvond_managed" and execution_status in {
            "setup_required",
            "adapter_required",
            "runtime_validation_required",
        }:
            schedule_status = str(item.get("schedule_status") or "").strip().lower()
            if schedule_status == "approval_required":
                blockers.append(f"{key}: automatic permission required for scheduled execution")
            elif schedule_status == "schedule_required":
                blockers.append(f"{key}: schedule configuration required")
            elif schedule_status == "schedule_setup_required":
                blockers.append(f"{key}: valid workspace timezone or schedule setup required")
            elif schedule_status == "runtime_inputs_required":
                missing = ", ".join(str(x) for x in (item.get("schedule_missing_inputs") or []))
                blockers.append(
                    f"{key}: scheduled runtime inputs required"
                    + (f" ({missing})" if missing else "")
                )
            elif schedule_status == "disabled":
                blockers.append(f"{key}: scheduled workflow is disabled")
            else:
                blockers.append(f"{key}: execution setup required")

    delivery = spec.get("delivery") if isinstance(spec.get("delivery"), dict) else {}
    graph_triggers = (
        [
            item
            for item in (delivery.get("graph_triggers") or [])
            if isinstance(item, dict)
        ]
        if isinstance(delivery.get("graph_triggers"), list)
        else []
    )
    if not graph_triggers:
        legacy_graph_trigger = delivery.get("graph_trigger")
        if isinstance(legacy_graph_trigger, dict):
            graph_triggers = [legacy_graph_trigger]

    for graph_trigger in graph_triggers:
        graph_status = str(
            graph_trigger.get("status") or "not_required"
        ).strip().lower()
        if graph_status in {"ready", "not_required", "managed_delivery"}:
            continue
        routine_name = str(
            graph_trigger.get("routine_name")
            or graph_trigger.get("routine_id")
            or "Execution graph"
        ).strip()
        prefix = f"{routine_name}: "
        if graph_status == "schedule_required":
            blockers.append(prefix + "schedule configuration required")
        elif graph_status == "schedule_setup_required":
            blockers.append(prefix + "valid workspace timezone or schedule setup required")
        elif graph_status == "runtime_input_conflict":
            conflicts = ", ".join(
                str(item)
                for item in (graph_trigger.get("runtime_input_conflicts") or [])
            )
            blockers.append(
                prefix
                + "conflicting runtime input keys"
                + (f" ({conflicts})" if conflicts else "")
            )
        elif graph_status == "disabled":
            blockers.append(prefix + "generated workflow is disabled")
        else:
            blockers.append(prefix + "trigger setup required")
    return blockers


def evaluate_readiness(
    *,
    subscribed: bool,
    channel_limit: int | None,
    requested_channels: list[str],
    enabled_channels: list[str],
    compiled_spec: dict | None,
    provisioned: bool,
    resolved_requirements: set[str] | None = None,
) -> dict:
    spec = compiled_spec or {}
    requested = communication_channels(requested_channels)
    required = _requirement_channel_keys(spec)

    active = communication_channels(enabled_channels)

    slot_channels = list(requested)
    for item in required:
        if item not in slot_channels:
            slot_channels.append(item)
    for item in active:
        if item not in slot_channels:
            slot_channels.append(item)

    # Xvond Workspace is a built-in communication surface and needs no
    # AgentChannel row. If it was requested/required, it is considered active.
    if "xvond" in slot_channels and "xvond" not in active:
        active.append("xvond")

    blockers: list[str] = []
    if not subscribed:
        blockers.append("Active AI Employee subscription required")
    if not spec:
        blockers.append("Build the employee before launch")
    elif not provisioned:
        blockers.append("Employee action plan is not provisioned")

    billed_slot_channels = [
        item
        for item in slot_channels
        if (get_channel_capability(item) or {}).get("channel_slot") is True
    ]
    if channel_limit is not None and len(billed_slot_channels) > channel_limit:
        blockers.append(
            f"Selected communication channels exceed the plan limit ({channel_limit})"
        )

    missing_channels = [item for item in slot_channels if item not in active]
    for item in missing_channels:
        blockers.append(f"Connect and activate {item} before launch")

    blockers.extend(
        _execution_blockers(
            spec,
            resolved_channels=active,
            resolved_requirements=resolved_requirements,
        )
    )

    mode = interaction_mode(spec, slot_channels)
    channels_required = bool(slot_channels)

    return {
        "ready": not blockers,
        "mode": mode,
        "channels_required": channels_required,
        "requested_channels": requested,
        "required_channels": required,
        "slot_channels": slot_channels,
        "active_channels": active,
        "missing_channels": missing_channels,
        "channel_limit": channel_limit,
        "channel_slots_used": len(billed_slot_channels),
        "billed_slot_channels": billed_slot_channels,
        "blockers": blockers,
    }


def self_service_readiness(
    db,
    *,
    company: Company,
    agent: AIAgent,
    config: AgentConfig,
) -> dict:
    if not is_self_service_company(company):
        raise HTTPException(
            409,
            "This employee belongs to Xvond Managed delivery and must use the managed launch flow",
        )

    builder = dict((config.settings or {}).get("employee_builder") or {})
    source = str(builder.get("onboarding_source") or company.onboarding_source or "").strip().lower()
    if source != SELF_SERVICE_SOURCE:
        raise HTTPException(
            409,
            "This employee is not a self-service employee",
        )

    spec = self_service_spec_view(builder.get("compiled_spec"))
    provisioned = bool(
        isinstance(spec, dict)
        and (spec.get("delivery") or {}).get("provisioning_version") == 1
    )

    billing = subscription_snapshot(db, company.id)
    active_channels = enabled_channel_types(
        db,
        company_id=company.id,
        agent_id=agent.id,
    )
    requested_channels = list(builder.get("requested_channels") or [])
    resolved_channels = active_channels
    prepared_channels: list[str] = []
    if not agent.enabled:
        desired = set(communication_channels(requested_channels))
        desired.update(_requirement_channel_keys(spec or {}))
        prepared_channels = [
            item
            for item in configured_channel_types(
                db,
                company_id=company.id,
                agent_id=agent.id,
            )
            if item in desired
        ]
        resolved_channels = prepared_channels

    resolved_requirements = _resolved_customer_requirement_keys(
        db,
        company_id=company.id,
        agent_id=agent.id,
    )
    connected_system_setup = _connected_system_setup(
        db,
        company_id=company.id,
        spec=spec or {},
    )
    billing_required = (
        settings.SELF_SERVICE_REQUIRE_SUBSCRIPTION
        and not settings.SELF_SERVICE_FREE_EXPERIMENT
    )
    state = evaluate_readiness(
        subscribed=bool(billing["active"]) or not billing_required,
        channel_limit=billing["channel_limit"] if billing_required else None,
        requested_channels=requested_channels,
        enabled_channels=resolved_channels,
        compiled_spec=spec,
        provisioned=provisioned,
        resolved_requirements=resolved_requirements,
    )
    state["connected_system_setup"] = connected_system_setup
    if connected_system_setup:
        state["ready"] = False
        state["blockers"].extend(
            item["message"] for item in connected_system_setup
        )
    provider_ready = _self_service_provider_ready(
        db,
        company_id=company.id,
        agent=agent,
    )
    state["provider_ready"] = provider_ready
    if not provider_ready:
        state["ready"] = False
        state["blockers"].append("At least one real AI provider/model must be available")
    state["resolved_requirements"] = sorted(resolved_requirements)
    state["active_channels"] = active_channels
    state["prepared_channels"] = prepared_channels
    state["subscription"] = {
        "required": billing_required,
        "active": bool(billing["active"]) or not billing_required,
        "status": billing.get("subscription_status"),
        "plan_name": billing["plan_name"],
        "plan_tier": billing["plan_tier"],
    }
    state["employee_source"] = SELF_SERVICE_SOURCE
    state["lifecycle"] = "live" if agent.enabled else "draft"
    return state


def _connected_system_setup(db, *, company_id: int, spec: dict) -> list[dict]:
    """Re-check customer-owned execution connections at every readiness read."""

    setup: list[dict] = []
    for requirement in spec.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        if requirement.get("validation_required") is not True:
            continue

        key = normalize_requirement_key(requirement.get("key")) or "connected_system"
        integration_id = requirement.get("integration_id")
        try:
            integration_id = int(integration_id)
        except (TypeError, ValueError):
            integration_id = 0

        integration = None
        if integration_id:
            integration = (
                db.query(CompanyIntegration)
                .filter(
                    CompanyIntegration.id == integration_id,
                    CompanyIntegration.company_id == company_id,
                    CompanyIntegration.enabled.is_(True),
                )
                .first()
            )

        label = str(requirement.get("purpose") or key.replace("_", " ")).strip()
        if integration is None:
            requirement["execution_status"] = "setup_required"
            message = f"Reconnect the connected system for {label} before launch"
            reason = "unavailable"
            name = None
        elif not integration_validation_ready(reveal_config(integration.config) or {}):
            requirement["execution_status"] = "setup_required"
            message = f"Validate {integration.name} again before launch"
            reason = "validation_required"
            name = integration.name
        else:
            if requirement.get("execution_status") == "setup_required":
                requirement["execution_status"] = "ready"
            continue

        setup.append(
            {
                "requirement_key": key,
                "integration_id": integration_id or None,
                "integration_name": name,
                "reason": reason,
                "message": message,
            }
        )
    return setup


def assert_self_service_runtime_subscription(db, *, company_id: int) -> None:
    """Protect paid self-service execution only when billing enforcement is enabled."""
    if (
        settings.is_test
        or settings.SELF_SERVICE_FREE_EXPERIMENT
        or not settings.SELF_SERVICE_REQUIRE_SUBSCRIPTION
    ):
        return
    company = db.query(Company).filter(Company.id == company_id).first()
    if not is_self_service_company(company):
        return
    service_limits.entitlement(db, company_id, "ai_agents")
