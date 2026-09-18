from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from backend.app.core.config.settings import settings
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.channels.models import AgentChannel


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
        return {
            "active": False,
            "subscription": None,
            "plan": None,
            "plan_name": None,
            "plan_tier": None,
            "channel_limit": None,
        }

    return {
        "active": True,
        "subscription": subscription,
        "plan": plan,
        "plan_name": plan.name,
        "plan_tier": plan.tier,
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


def _execution_blockers(spec: dict) -> list[str]:
    blockers: list[str] = []
    for item in spec.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "requirement").strip()
        status = str(item.get("status") or "").strip().lower()
        execution_status = str(item.get("execution_status") or "").strip().lower()

        if status in {"connection_required", "customer_input_required", "setup_required"}:
            blockers.append(f"{key}: setup required")
            continue
        if status == "xvond_managed" and execution_status in {
            "setup_required",
            "adapter_required",
            "runtime_validation_required",
        }:
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
) -> dict:
    spec = compiled_spec or {}
    requested = communication_channels(requested_channels)
    required = _requirement_channel_keys(spec)

    slot_channels = list(requested)
    for item in required:
        if item not in slot_channels:
            slot_channels.append(item)

    active = communication_channels(enabled_channels)
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

    blockers.extend(_execution_blockers(spec))

    mode = interaction_mode(spec, requested)
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

    spec = builder.get("compiled_spec")
    if not isinstance(spec, dict):
        spec = None
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
    state = evaluate_readiness(
        subscribed=bool(billing["active"]),
        channel_limit=billing["channel_limit"],
        requested_channels=list(builder.get("requested_channels") or []),
        enabled_channels=active_channels,
        compiled_spec=spec,
        provisioned=provisioned,
    )
    state["subscription"] = {
        "active": bool(billing["active"]),
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
