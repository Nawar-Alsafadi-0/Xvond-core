from __future__ import annotations

from copy import deepcopy

from backend.app.core.config_secrets import reveal_config
from backend.app.modules.ai_agent.employee_compiler import normalize_requirement_key
from backend.app.modules.tools.models import AgentToolAssignment


MANAGED_STATUS = "xvond_managed"
BUILD_STATUS = "xvond_build"
CUSTOMER_STATUSES = {"connection_required", "customer_input_required"}


def _permission_mode(spec: dict, requirement: dict) -> str:
    """Only an exact capability/purpose rule can grant automatic execution."""
    targets = {
        str(requirement.get(field) or "").strip().lower().replace("_", " ")
        for field in ("purpose", "key")
    } - {""}
    modes = []
    for item in spec.get("permissions") or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().lower().replace("_", " ")
        mode = str(item.get("mode") or "ask_before").strip().lower()
        if not action:
            continue
        if action in targets:
            modes.append(mode)
    if "never" in modes:
        return "never"
    return "automatic" if modes and all(mode == "automatic" for mode in modes) else "ask_before"


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
        "enabled": _permission_mode(spec, requirement) != "never",
        "label": purpose[:200] or key,
        "description": purpose[:1000],
        "module": "tools",
        "fields": [],
        "confirmation_required": _permission_mode(spec, requirement) != "automatic",
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
        if item.get("status") == "available"
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
    assignment = (
        db.query(AgentToolAssignment)
        .filter(
            AgentToolAssignment.agent_id == agent_id,
            AgentToolAssignment.tool_name == "action_request",
        )
        .with_for_update()
        .first()
    )
    config = reveal_config(assignment.config) if assignment is not None else {}
    config = dict(config or {})
    actions = dict(config.get("actions") or {})
    action_plan = {}
    changed = False

    for item in requirements:
        if not isinstance(item, dict):
            continue
        # Upgrade cached pre-builder specs without another paid AI call.
        aliases = {
            "custom_required": BUILD_STATUS,
            "unsupported": BUILD_STATUS,
            "customer_connection": "connection_required",
            "customer_input": "customer_input_required",
        }
        item["status"] = aliases.get(item.get("status"), item.get("status"))
        if item.get("status") not in {BUILD_STATUS, MANAGED_STATUS}:
            continue
        key = normalize_requirement_key(item.get("key"))
        if not key:
            continue
        item["key"] = key
        if not isinstance(actions.get(key), dict):
            actions[key] = build_managed_action_config(requirement=item, spec=prepared)
            changed = True
        # Existing operator configuration, permissions and disable switches win.
        action = actions[key]
        execution_status = (
            "disabled" if not action.get("enabled", True) or (assignment is not None and not assignment.enabled)
            else "adapter_required" if (action.get("destination") or {}).get("type") == "workflow_engine"
            else "runtime_validation_required"
        )
        item["status"] = MANAGED_STATUS
        item["provisioned"] = True
        item["delivery_mode"] = "compose"
        item["execution_status"] = execution_status
        action_plan[key] = {
            "tool_name": "action_request",
            "action_type": key,
            "operations": [f"{key}.{operation}" for operation in ("check_availability", "execute", "cancel")],
            "status": "contract_provisioned",
            "execution_status": execution_status,
            "source": "generated" if action.get("xvond_generated") else "existing",
        }

    if changed:
        config["actions"] = actions
        if assignment is None:
            assignment = AgentToolAssignment(
                agent_id=agent_id,
                tool_name="action_request",
                config=config,
                enabled=True,
            )
            db.add(assignment)
        else:
            assignment.config = config

    _refresh_delivery_fields(prepared)
    delivery = {
        "provisioning_version": 1,
        "action_plan": action_plan,
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
