from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import reveal_config
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.channels.catalog import validate_channel_config
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument


SELF_SERVICE_SOURCE = "self_service"

# Only communication surfaces consume a channel slot. Gmail, email read/send,
# Instagram publishing and similar systems are integrations, not chat channels.
COMMUNICATION_CHANNELS = frozenset(
    {
        "xvond",
        "website",
        "whatsapp",
        "voice",
        "telegram",
        "custom",
    }
)

EXTERNAL_COMMUNICATION_CHANNELS = COMMUNICATION_CHANNELS - {"xvond"}
SELF_SERVICE_LIVE_EXTERNAL_CHANNELS = frozenset({"whatsapp", "website"})
SELF_SERVICE_DIRECT_CONNECTION_REQUIREMENTS = SELF_SERVICE_LIVE_EXTERNAL_CHANNELS


def is_self_service_company(company: Company | None) -> bool:
    return bool(
        company is not None
        and str(company.onboarding_source or "").strip().lower() == SELF_SERVICE_SOURCE
    )


def communication_channels(values: Any) -> list[str]:
    result: list[str] = []
    for item in values or []:
        key = str(item or "").strip().lower()
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
    key = str(channel_type or "").strip().lower()
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
        key = str(item.get("key") or "").strip().lower()
        if key == "xvond_workspace":
            key = "xvond"
        if key in COMMUNICATION_CHANNELS and key not in result:
            result.append(key)
    return result


def self_service_connection_status(item: dict) -> str | None:
    status = str(item.get("status") or "").strip().lower()
    if status != "connection_required":
        return None
    key = str(item.get("key") or "").strip().lower()
    kind = str(item.get("kind") or "").strip().lower()
    if kind == "channel" and key in SELF_SERVICE_DIRECT_CONNECTION_REQUIREMENTS:
        return "self_service_available"
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
        key = str(row.channel_type or "").strip().lower()
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
        key = str(row.channel_type or "").strip().lower()
        if key not in SELF_SERVICE_LIVE_EXTERNAL_CHANNELS or key in result:
            continue
        try:
            validate_channel_config(key, reveal_config(row.config) or {})
        except ValueError:
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
    channel_type = str(channel.channel_type or "").strip().lower()

    if channel.company_id != company.id or channel.agent_id != agent.id:
        return ["Communication channel ownership does not match this employee"]
    if channel_type not in SELF_SERVICE_LIVE_EXTERNAL_CHANNELS:
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
        blockers.append(f"{channel_type.title()} channel configuration is incomplete")
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

    return blockers


def _resolved_customer_requirement_keys(
    db,
    *,
    company_id: int,
    agent_id: int,
) -> set[str]:
    resolved: set[str] = set()
    knowledge_count = (
        db.query(AgentKnowledge)
        .join(KnowledgeDocument, KnowledgeDocument.id == AgentKnowledge.document_id)
        .filter(
            AgentKnowledge.agent_id == agent_id,
            AgentKnowledge.enabled.is_(True),
            KnowledgeDocument.company_id == company_id,
            KnowledgeDocument.enabled.is_(True),
        )
        .count()
    )
    if knowledge_count > 0:
        resolved.add("knowledge")
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
        channel_key = "xvond" if key == "xvond_workspace" else key.lower()
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

    if channel_limit is not None and len(slot_channels) > channel_limit:
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
        "channel_slots_used": len(slot_channels),
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
    state = evaluate_readiness(
        subscribed=bool(billing["active"]),
        channel_limit=billing["channel_limit"],
        requested_channels=requested_channels,
        enabled_channels=resolved_channels,
        compiled_spec=spec,
        provisioned=provisioned,
        resolved_requirements=resolved_requirements,
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
        "active": bool(billing["active"]),
        "status": billing.get("subscription_status"),
        "plan_name": billing["plan_name"],
        "plan_tier": billing["plan_tier"],
    }
    state["employee_source"] = SELF_SERVICE_SOURCE
    state["lifecycle"] = "live" if agent.enabled else "draft"
    return state


def assert_self_service_runtime_subscription(db, *, company_id: int) -> None:
    """Protect paid/background self-service execution without changing Managed behavior."""
    if settings.is_test:
        return
    company = db.query(Company).filter(Company.id == company_id).first()
    if not is_self_service_company(company):
        return
    service_limits.entitlement(db, company_id, "ai_agents")
