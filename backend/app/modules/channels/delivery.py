from __future__ import annotations

from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.modules.channels.catalog import (
    CHANNEL_SETUP_MANAGED,
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
        if row.enabled:
            row.enabled = False
        if state not in {"cancelled", "connected"}:
            row.config = merge_config(
                row.config,
                {
                    "provisioning_state": "cancelled",
                    "provisioning_error": None,
                },
            )
            cancelled.append(channel_type)

    db.flush()
    return {
        "desired": sorted(desired),
        "requested": requested,
        "cancelled": cancelled,
    }
