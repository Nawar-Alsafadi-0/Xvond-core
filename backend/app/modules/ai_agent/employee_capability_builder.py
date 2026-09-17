from __future__ import annotations

from copy import deepcopy

from backend.app.core.config_secrets import reveal_config
from backend.app.modules.tools.models import AgentToolAssignment


MANAGED_STATUS = "xvond_managed"
BUILD_STATUS = "xvond_build"
CUSTOMER_STATUSES = {"connection_required", "customer_input_required"}


def _permission_requires_confirmation(spec: dict, requirement: dict) -> bool:
    """Default consequential generated actions to approval unless explicitly automatic."""
    purpose = str(requirement.get("purpose") or requirement.get("key") or "").lower()
    automatic_matches = []
    for item in spec.get("permissions") or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower()
        mode = str(item.get("mode") or "ask_before").strip().lower()
        if not action:
            continue
        if action in purpose or any(token and token in purpose for token in action.split()):
            automatic_matches.append(mode == "automatic")
    return not (automatic_matches and all(automatic_matches))


def build_managed_action_config(*, requirement: dict, spec: dict) -> dict:
    """Compile one novel capability into Xvond's generic workflow action contract.

    The workflow engine is the execution primitive. The customer does not need a
    pre-existing named Xvond feature for the job to be representable.
    """
    key = str(requirement.get("key") or "managed_capability").strip()
    purpose = str(requirement.get("purpose") or key.replace("_", " ")).strip()
    primitives = [
        str(item).strip()
        for item in (requirement.get("primitives") or ["workflow_engine"])
        if str(item).strip()
    ]
    return {
        "enabled": True,
        "label": purpose[:200] or key,
        "description": purpose[:1000],
        "module": "tools",
        "fields": [],
        "confirmation_required": _permission_requires_confirmation(spec, requirement),
        "destination": {
            "type": "workflow_engine",
            "capability_key": key,
            "delivery_mode": "compose",
            "primitives": primitives,
            "job_summary": str(spec.get("summary") or "")[:1000],
            "job_brief": str(spec.get("job_brief") or "")[:2000],
        },
        "availability": {"mode": "none"},
        "xvond_generated": True,
    }


def _refresh_delivery_fields(spec: dict) -> dict:
    requirements = spec.get("requirements") or []
    spec["ready_requirements"] = [
        item.get("key")
        for item in requirements
        if item.get("status") in {"available", MANAGED_STATUS}
    ]
    spec["build_required"] = [
        item.get("key") for item in requirements if item.get("status") == BUILD_STATUS
    ]
    spec["setup_required"] = [
        item.get("key") for item in requirements if item.get("status") in CUSTOMER_STATUSES
    ]
    spec["unsupported_requirements"] = []
    return spec


def provision_compiled_capabilities(db, *, agent_id: int, spec: dict) -> tuple[dict, dict]:
    """Provision open-ended requirements into the generic Xvond execution plane.

    Unknown digital work becomes an Xvond-managed workflow contract rather than
    an unsupported/custom requirement. External accounts can still require the
    customer's credentials or platform permission before execution.
    """
    prepared = deepcopy(spec)
    requirements = prepared.get("requirements") or []
    generated_actions: dict[str, dict] = {}

    for item in requirements:
        if not isinstance(item, dict) or item.get("status") != BUILD_STATUS:
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        generated_actions[key] = build_managed_action_config(requirement=item, spec=prepared)
        item["status"] = MANAGED_STATUS
        item["provisioned"] = True
        item["delivery_mode"] = "compose"

    if generated_actions:
        assignment = (
            db.query(AgentToolAssignment)
            .filter(
                AgentToolAssignment.agent_id == agent_id,
                AgentToolAssignment.tool_name == "action_request",
            )
            .first()
        )
        if assignment is None:
            assignment = AgentToolAssignment(
                agent_id=agent_id,
                tool_name="action_request",
                config={"actions": generated_actions},
                enabled=True,
            )
            db.add(assignment)
        else:
            existing = reveal_config(assignment.config) or {}
            config = dict(existing)
            actions = dict(config.get("actions") or {})
            for key, value in generated_actions.items():
                current = actions.get(key)
                if isinstance(current, dict) and not current.get("xvond_generated"):
                    continue
                actions[key] = value
            config["actions"] = actions
            assignment.config = config
            assignment.enabled = True

    _refresh_delivery_fields(prepared)
    delivery = {
        "managed_capabilities": [
            item.get("key")
            for item in requirements
            if item.get("status") == MANAGED_STATUS
        ],
        "connection_required": [
            item.get("key")
            for item in requirements
            if item.get("status") == "connection_required"
        ],
        "customer_input_required": [
            item.get("key")
            for item in requirements
            if item.get("status") == "customer_input_required"
        ],
        "unsupported": [],
    }
    prepared["delivery"] = delivery
    return prepared, delivery
