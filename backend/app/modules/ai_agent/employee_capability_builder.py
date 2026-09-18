from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlparse

from backend.app.core.config_secrets import reveal_config
from backend.app.models.company import Company
from backend.app.models.company_profile import CompanyProfile
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.automation.schedule import ScheduleConfigError, normalize_schedule_config
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


def _grounded_https_hosts(spec: dict) -> list[str]:
    """Extract only HTTPS hosts that the customer actually wrote in the Job Brief."""
    text = str(spec.get("job_brief") or "")
    hosts: list[str] = []
    for raw in re.findall(r"https://[^\s<>'\"]+", text, flags=re.IGNORECASE):
        parsed = urlparse(raw.rstrip(".,);]}"))
        host = str(parsed.hostname or "").rstrip(".").lower()
        if host and host not in hosts:
            hosts.append(host)
        if len(hosts) >= 20:
            break
    return hosts


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
            "type": "xvond_internal",
            "adapter": "generic_capability",
            "capability_key": key,
            "delivery_mode": "compose",
            "primitives": primitives,
            "execution_plan": [
                dict(step)
                for step in (requirement.get("execution_plan") or [])
                if isinstance(step, dict)
            ][:20],
            "allowed_hosts": _grounded_https_hosts(spec),
            "job_summary": str(spec.get("summary") or "")[:1000],
            "job_brief": str(spec.get("job_brief") or "")[:2000],
            "runtime_inputs": dict(requirement.get("runtime_inputs") or {}),
        },
        "availability": {"mode": "none"},
        "xvond_generated": True,
    }


def _scheduled_runtime_fields(requirement: dict) -> list[str]:
    result: list[str] = []
    for step in requirement.get("execution_plan") or []:
        if not isinstance(step, dict):
            continue
        field = None
        if step.get("op") == "http_get_json":
            field = step.get("url_field")
        elif step.get("op") == "compare":
            field = step.get("value_field")
        key = normalize_requirement_key(field)
        if key and key not in result:
            result.append(key)
    return result


def _company_context(db, agent_id: int) -> tuple[Company | None, str | None]:
    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if agent is None:
        return None, None
    company = db.query(Company).filter(Company.id == agent.company_id).first()
    profile = (
        db.query(CompanyProfile)
        .filter(CompanyProfile.company_id == agent.company_id)
        .first()
    )
    timezone = str(profile.timezone or "").strip() if profile is not None else None
    return company, timezone or None


def _generated_schedule_workflow(
    db,
    *,
    company_id: int,
    agent_id: int,
    requirement_key: str,
) -> AutomationWorkflow | None:
    rows = (
        db.query(AutomationWorkflow)
        .filter(
            AutomationWorkflow.company_id == company_id,
            AutomationWorkflow.trigger_type == "schedule",
        )
        .all()
    )
    for row in rows:
        config = row.trigger_config if isinstance(row.trigger_config, dict) else {}
        if (
            config.get("_xvond_source") == "self_service_employee"
            and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
            and str(config.get("_xvond_requirement_key") or "") == requirement_key
        ):
            return row
    return None


def _provision_self_service_schedule(
    db,
    *,
    company: Company,
    timezone: str | None,
    agent_id: int,
    requirement: dict,
    action: dict,
) -> tuple[str, int | None]:
    if str(company.onboarding_source or "").strip().lower() != "self_service":
        return "managed_delivery", None
    if "scheduler" not in (requirement.get("primitives") or []):
        return "not_required", None
    if action.get("confirmation_required", True):
        return "approval_required", None
    raw_schedule = requirement.get("schedule")
    if not isinstance(raw_schedule, dict):
        return "schedule_required", None

    try:
        schedule = normalize_schedule_config(
            raw_schedule,
            default_timezone=timezone,
        )
    except ScheduleConfigError:
        return "schedule_setup_required", None

    runtime_inputs = dict(requirement.get("runtime_inputs") or {})
    missing = [
        key
        for key in _scheduled_runtime_fields(requirement)
        if key not in runtime_inputs
    ]
    if missing:
        requirement["schedule_missing_inputs"] = missing
        return "runtime_inputs_required", None

    key = str(requirement.get("key") or "")
    workflow = _generated_schedule_workflow(
        db,
        company_id=company.id,
        agent_id=agent_id,
        requirement_key=key,
    )
    if workflow is not None and not workflow.enabled:
        return "disabled", workflow.id
    if workflow is None:
        workflow = AutomationWorkflow(
            company_id=company.id,
            name=(str(requirement.get("purpose") or key.replace("_", " ")) or "Scheduled employee task")[:200],
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": agent_id,
                "_xvond_requirement_key": key,
                "_xvond_generated": True,
                "schedule": schedule,
                "input_data": runtime_inputs,
            },
            steps=[
                {
                    "type": "scheduled_action",
                    "agent_id": agent_id,
                    "action_type": key,
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.flush()
    return "ready", workflow.id


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
    automation_plan = {}
    changed = False
    company, company_timezone = _company_context(db, agent_id)
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
        destination = action.get("destination") or {}
        if not action.get("enabled", True) or (assignment is not None and not assignment.enabled):
            execution_status = "disabled"
        elif destination.get("type") == "xvond_internal" and destination.get("adapter") == "generic_capability":
            plan = destination.get("execution_plan") or []
            needs_http = any(
                isinstance(step, dict) and step.get("op") == "http_get_json"
                for step in plan
            )
            execution_status = (
                "ready"
                if plan and (not needs_http or destination.get("allowed_hosts"))
                else "setup_required"
            )
        elif destination.get("type") == "workflow_engine":
            execution_status = "adapter_required"
        else:
            execution_status = "runtime_validation_required"
        schedule_status = "not_required"
        schedule_workflow_id = None
        if (
            company is not None
            and execution_status == "ready"
            and "scheduler" in (item.get("primitives") or [])
        ):
            schedule_status, schedule_workflow_id = _provision_self_service_schedule(
                db,
                company=company,
                timezone=company_timezone,
                agent_id=agent_id,
                requirement=item,
                action=action,
            )
            if schedule_status not in {"ready", "not_required", "managed_delivery"}:
                execution_status = "setup_required"
        item["status"] = MANAGED_STATUS
        item["provisioned"] = True
        item["delivery_mode"] = "compose"
        item["execution_status"] = execution_status
        if schedule_status != "not_required":
            item["schedule_status"] = schedule_status
        if schedule_workflow_id is not None:
            item["schedule_workflow_id"] = schedule_workflow_id
            automation_plan[key] = {
                "workflow_id": schedule_workflow_id,
                "status": schedule_status,
            }
        action_plan[key] = {
            "tool_name": "action_request",
            "action_type": key,
            "operations": [f"{key}.{operation}" for operation in ("check_availability", "execute", "cancel")],
            "status": "contract_provisioned",
            "execution_status": execution_status,
            "source": "generated" if action.get("xvond_generated") else "existing",
            "schedule_status": schedule_status,
            "automation_workflow_id": schedule_workflow_id,
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
        "automation_plan": automation_plan,
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
