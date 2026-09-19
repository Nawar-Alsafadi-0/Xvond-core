from __future__ import annotations

from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.modules.channels.catalog import (
    CHANNEL_SETUP_MANAGED,
    N8N_CHANNEL_ADAPTER,
    canonical_channel_type,
    get_channel_capability,
)
from backend.app.modules.channels.models import AgentChannel


def _managed_types(values) -> list[str]:
    result: list[str] = []
    for raw in values or []:
        key = canonical_channel_type(raw)
        capability = get_channel_capability(key)
        if (
            capability is None
            or capability.get("setup_mode") != CHANNEL_SETUP_MANAGED
            or key in result
        ):
            continue
        result.append(key)
    return result


def deactivate_managed_channel_route(channel: AgentChannel) -> dict:
    """Best-effort cleanup of one workflow-plane route.

    Disabling a channel is only a pause and does not call this helper. Removing a
    managed channel from the employee contract or deleting the channel does.
    """

    capability = get_channel_capability(channel.channel_type) or {}
    if (
        capability.get("setup_mode") != CHANNEL_SETUP_MANAGED
        or capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
    ):
        return {"required": False, "complete": True, "reason": "not_applicable"}

    config = reveal_config(channel.config) or {}
    connection_key = str(config.get("connection_key") or "").strip()
    state = str(config.get("provisioning_state") or "").strip().lower()
    cleanup_state = str(config.get("registry_cleanup_state") or "").strip().lower()
    route_may_exist = state == "connected" or cleanup_state == "pending"
    if not route_may_exist:
        return {"required": False, "complete": True, "reason": "no_live_route"}
    if not connection_key:
        return {"required": True, "complete": False, "reason": "connection_key_missing"}
    if not n8n_gateway.configured():
        return {"required": True, "complete": False, "reason": "gateway_unavailable"}

    try:
        result = n8n_gateway.deactivate_channel(
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            channel_id=channel.id,
            connection_key=connection_key,
        )
    except N8NGatewayError:
        return {"required": True, "complete": False, "reason": "registry_cleanup_failed"}

    if result.get("success") is not True:
        return {"required": True, "complete": False, "reason": "registry_cleanup_failed"}
    return {
        "required": True,
        "complete": True,
        "reason": "deactivated",
        "deactivated": bool((result.get("data") or {}).get("deactivated")),
    }


def reconcile_managed_channel_requests(
    db,
    *,
    company_id: int,
    agent_id: int,
    desired_channel_types,
    request_source: str = "employee_builder",
) -> dict:
    """Keep Xvond-managed channel requests aligned with the current employee contract.

    A managed request is not a live channel. It is represented by a disabled
    AgentChannel row so the admin provisioning surfaces have a durable work item.
    Existing provider credentials/provisioning evidence are never overwritten.
    """

    desired = set(_managed_types(desired_channel_types))
    rows = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == company_id,
            AgentChannel.agent_id == agent_id,
        )
        .all()
    )
    by_type = {
        canonical_channel_type(row.channel_type): row
        for row in rows
    }

    requested: list[str] = []
    cancelled: list[str] = []

    for channel_type in sorted(desired):
        row = by_type.get(channel_type)
        if row is None:
            row = AgentChannel(
                company_id=company_id,
                agent_id=agent_id,
                channel_type=channel_type,
                config={
                    "provisioning_state": "requested",
                    "request_source": request_source,
                },
                enabled=False,
            )
            db.add(row)
            by_type[channel_type] = row
            requested.append(channel_type)
            continue

        config = reveal_config(row.config) or {}
        state = str(config.get("provisioning_state") or "").strip().lower()
        if state in {"", "cancelled"}:
            row.config = merge_config(
                row.config,
                {
                    "provisioning_state": "requested",
                    "request_source": request_source,
                    "provisioning_error": None,
                },
            )
            row.enabled = False
            requested.append(channel_type)

    for channel_type, row in by_type.items():
        capability = get_channel_capability(channel_type)
        if (
            capability is None
            or capability.get("setup_mode") != CHANNEL_SETUP_MANAGED
            or channel_type in desired
        ):
            continue

        config = reveal_config(row.config) or {}
        state = str(config.get("provisioning_state") or "").strip().lower()
        cleanup_state = str(config.get("registry_cleanup_state") or "").strip().lower()
        if row.enabled:
            row.enabled = False

        cleanup = deactivate_managed_channel_route(row)
        needs_state_update = (
            state != "cancelled"
            or cleanup_state == "pending"
            or cleanup.get("required") is True
        )
        if needs_state_update:
            cleanup_complete = cleanup.get("complete") is True
            row.config = merge_config(
                row.config,
                {
                    "provisioning_state": "cancelled",
                    "registry_cleanup_state": (
                        "complete"
                        if cleanup_complete
                        else "pending"
                    ),
                    "provisioning_error": (
                        None
                        if cleanup_complete
                        else "workflow_registry_cleanup_pending"
                    ),
                },
            )
        if state != "cancelled" or cleanup_state == "pending":
            cancelled.append(channel_type)

    db.flush()
    return {
        "desired": sorted(desired),
        "requested": requested,
        "cancelled": cancelled,
    }
