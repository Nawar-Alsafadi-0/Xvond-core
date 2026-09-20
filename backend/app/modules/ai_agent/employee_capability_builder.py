from __future__ import annotations

from copy import deepcopy
import re
from urllib.parse import urlparse

from backend.app.core.config_secrets import reveal_config
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.company_profile import CompanyProfile
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.integrations.catalog import integration_validation_ready
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.automation.execution_graph import (
    graph_action_types,
    graph_contract_errors,
    normalize_execution_graph,
)
from backend.app.modules.automation.schedule import ScheduleConfigError, normalize_schedule_config
from backend.app.modules.ai_agent.employee_compiler import normalize_requirement_key
from backend.app.modules.tools.generic_capability_runtime import generic_capability_readiness
from backend.app.modules.tools.models import AgentToolAssignment


MANAGED_STATUS = "xvond_managed"
BUILD_STATUS = "xvond_build"
CUSTOMER_STATUSES = {"connection_required", "customer_input_required"}


_WEEKDAY_ALIASES = {
    "monday": 0, "mon": 0, "الاثنين": 0, "الإثنين": 0,
    "tuesday": 1, "tue": 1, "الثلاثاء": 1,
    "wednesday": 2, "wed": 2, "الأربعاء": 2, "الاربعاء": 2,
    "thursday": 3, "thu": 3, "الخميس": 3,
    "friday": 4, "fri": 4, "الجمعة": 4,
    "saturday": 5, "sat": 5, "السبت": 5,
    "sunday": 6, "sun": 6, "الأحد": 6, "الاحد": 6,
}


def _requirement_inputs(spec: dict, requirement: dict) -> dict:
    values = dict(requirement.get("runtime_inputs") or {})
    answers = spec.get("customer_inputs") or {}
    if isinstance(answers, dict):
        item = answers.get(str(requirement.get("key") or "").strip())
        if isinstance(item, dict):
            for key, value in item.items():
                if str(value or "").strip():
                    values[str(key).strip()] = str(value).strip()
    return values


def _parse_weekdays(value) -> list[int]:
    if isinstance(value, list):
        source = " ".join(str(item or "") for item in value)
    else:
        source = str(value or "")
    normalized = " ".join(source.strip().lower().replace("،", ",").split())
    result: list[int] = []

    # Support a common contiguous range such as Sunday-Thursday.
    range_match = re.search(
        r"([A-Za-z]+|الأحد|الاحد|الاثنين|الإثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة|السبت)\s*[-–—]\s*"
        r"([A-Za-z]+|الأحد|الاحد|الاثنين|الإثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة|السبت)",
        normalized,
        flags=re.IGNORECASE,
    )
    if range_match:
        start = _WEEKDAY_ALIASES.get(range_match.group(1).lower())
        end = _WEEKDAY_ALIASES.get(range_match.group(2).lower())
        if start is not None and end is not None:
            cursor = start
            for _ in range(7):
                if cursor not in result:
                    result.append(cursor)
                if cursor == end:
                    break
                cursor = (cursor + 1) % 7

    for alias, day in _WEEKDAY_ALIASES.items():
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized, flags=re.IGNORECASE):
            if day not in result:
                result.append(day)

    for token in re.findall(r"(?<!\d)([0-6])(?!\d)", normalized):
        day = int(token)
        if day not in result:
            result.append(day)
    return sorted(result)


def _valid_hhmm(value) -> str | None:
    raw = str(value or "").strip().lower().replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?", raw)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    elif not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def build_internal_booking_action_config(*, requirement: dict, spec: dict) -> dict:
    values = _requirement_inputs(spec, requirement)
    weekdays = _parse_weekdays(values.get("working_days"))
    start = _valid_hhmm(values.get("opening_time"))
    end = _valid_hhmm(values.get("closing_time"))
    try:
        slot_match = re.search(r"\d+", str(values.get("slot_minutes") or ""))
        slot_minutes = int(slot_match.group(0)) if slot_match else 0
    except ValueError:
        slot_minutes = 0
    slot_minutes = slot_minutes if 5 <= slot_minutes <= 720 else 0
    try:
        capacity_match = re.search(r"\d+", str(values.get("capacity") or ""))
        capacity = int(capacity_match.group(0)) if capacity_match else 1
    except ValueError:
        capacity = 1
    capacity = max(1, min(capacity, 100))

    schedule_ready = bool(weekdays and start and end and slot_minutes)
    fields = [
        {"key": "customer_name", "label": "Customer name", "required": True, "type": "text"},
        {"key": "phone", "label": "Phone", "required": True, "type": "phone"},
        {"key": "service", "label": "Service", "required": True, "type": "text"},
        {"key": "date", "label": "Date", "required": True, "type": "date", "role": "date"},
        {"key": "time", "label": "Time", "required": True, "type": "time", "role": "time"},
        {"key": "notes", "label": "Notes", "required": False, "type": "text"},
    ]
    action = {
        "enabled": True,
        "_xvond_permission_mode": _permission_mode(spec, requirement),
        "label": str(requirement.get("purpose") or "Booking").strip()[:200] or "Booking",
        "description": str(requirement.get("purpose") or "Create and manage bookings").strip()[:1000],
        "module": "booking",
        "fields": fields,
        "confirmation_required": _permission_mode(spec, requirement) != "automatic",
        "destination": {
            "type": "xvond_internal",
            "adapter": "booking",
            "delivery_mode": "native",
        },
        "availability": {
            "mode": "xvond_schedule" if schedule_ready else "none",
            "date_field": "date",
            "time_field": "time",
            "schedule": {
                "weekdays": weekdays,
                "start": start,
                "end": end,
                "slot_minutes": slot_minutes or None,
                "capacity": capacity,
            },
        },
        "xvond_generated": True,
    }
    action["_xvond_booking_setup_ready"] = schedule_ready
    return action


def build_internal_record_action_config(*, requirement: dict, spec: dict) -> dict:
    key = str(requirement.get("key") or "business_request").strip()
    purpose = str(requirement.get("purpose") or key.replace("_", " ")).strip()
    definitions = {
        "lead_management": {
            "module": "lead_management",
            "fields": [
                {"key": "customer_name", "label": "Customer name", "required": True, "type": "text"},
                {"key": "phone", "label": "Phone", "required": False, "type": "phone"},
                {"key": "email", "label": "Email", "required": False, "type": "email"},
                {"key": "interest", "label": "Interest", "required": True, "type": "text"},
                {"key": "notes", "label": "Notes", "required": False, "type": "text"},
            ],
        },
        "orders": {
            "module": "orders",
            "fields": [
                {"key": "customer_name", "label": "Customer name", "required": True, "type": "text"},
                {"key": "phone", "label": "Phone", "required": True, "type": "phone"},
                {"key": "items", "label": "Order items", "required": True, "type": "text"},
                {"key": "address", "label": "Delivery / pickup details", "required": False, "type": "text"},
                {"key": "notes", "label": "Notes", "required": False, "type": "text"},
            ],
        },
        "quotation": {
            "module": "quotation",
            "fields": [
                {"key": "customer_name", "label": "Customer name", "required": True, "type": "text"},
                {"key": "contact", "label": "Contact", "required": True, "type": "text"},
                {"key": "request", "label": "Quotation request", "required": True, "type": "text"},
                {"key": "notes", "label": "Notes", "required": False, "type": "text"},
            ],
        },
        "customer_support": {
            "module": "customer_support",
            "fields": [
                {"key": "customer_name", "label": "Customer name", "required": False, "type": "text"},
                {"key": "contact", "label": "Contact", "required": False, "type": "text"},
                {"key": "issue", "label": "Issue", "required": True, "type": "text"},
                {"key": "priority", "label": "Priority", "required": False, "type": "text"},
            ],
        },
    }
    definition = definitions.get(key)
    if definition is None:
        raise ValueError(f"No Xvond-native record definition for {key}")
    return {
        "enabled": True,
        "_xvond_permission_mode": _permission_mode(spec, requirement),
        "label": purpose[:200] or key,
        "description": purpose[:1000],
        "module": definition["module"],
        "fields": definition["fields"],
        "confirmation_required": _permission_mode(spec, requirement) != "automatic",
        "destination": {
            "type": "xvond_internal",
            "adapter": "business_record",
            "record_type": key,
            "delivery_mode": "native",
        },
        "availability": {"mode": "none"},
        "xvond_generated": True,
    }


def _external_graph_operations(spec: dict, requirement_key: str) -> list[str]:
    """Return explicit external operations selected by this requirement's graphs."""
    key = normalize_requirement_key(requirement_key)
    result: list[str] = []

    def visit(graph: dict) -> None:
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if not isinstance(nodes, list):
            return
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            node_type = str(raw.get("type") or "").strip().lower()
            params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
            if (
                node_type == "action"
                and normalize_requirement_key(params.get("action_type")) == key
            ):
                operation = normalize_requirement_key(params.get("operation"))
                if operation and operation not in result:
                    result.append(operation)
            elif node_type == "foreach":
                nested = params.get("graph")
                if isinstance(nested, dict):
                    visit(nested)

    for routine in _compiled_execution_routines(spec):
        if isinstance(routine, dict):
            visit(routine.get("graph") or {})
    return result


def _default_external_operation(*, requirement_key: str, operations: dict, spec: dict) -> str | None:
    """Choose a direct-chat operation only when the contract makes it unambiguous."""
    available = {
        normalize_requirement_key(name): str(name)
        for name, value in (operations or {}).items()
        if normalize_requirement_key(name) and isinstance(value, dict)
    }
    if not available:
        return None
    if "execute" in available:
        return available["execute"]

    graph_operations = [
        available[name]
        for name in _external_graph_operations(spec, requirement_key)
        if name in available
    ]
    graph_operations = list(dict.fromkeys(graph_operations))
    if len(graph_operations) == 1:
        return graph_operations[0]
    if len(available) == 1:
        return next(iter(available.values()))
    return None


def build_external_integration_action_config(*, requirement: dict, spec: dict) -> dict:
    key = str(requirement.get("key") or "connected_action").strip()
    purpose = str(requirement.get("purpose") or key.replace("_", " ")).strip()
    integration_id = requirement.get("integration_id")
    operations = requirement.get("integration_operations")
    operations = dict(operations) if isinstance(operations, dict) else {}

    fields = []
    availability = {"mode": "none"}
    default_operation = _default_external_operation(
        requirement_key=key,
        operations=operations,
        spec=spec,
    )
    execute_operation = (
        operations.get(default_operation)
        if default_operation and isinstance(operations.get(default_operation), dict)
        else {}
    )
    seen_fields: set[str] = set()

    def add_field(
        raw_key,
        *,
        required: bool = True,
        raw_type: str = "string",
        raw_format: str = "",
    ) -> None:
        field_key = str(raw_key or "").strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", field_key)
            or field_key in seen_fields
        ):
            return
        field_type = "text"
        value_type = str(raw_type or "").strip().lower()
        value_format = str(raw_format or "").strip().lower()
        if value_format == "binary":
            field_type = "file"
        elif value_format == "email":
            field_type = "email"
        elif value_format == "date":
            field_type = "date"
        elif value_format == "time":
            field_type = "time"
        elif value_format in {"phone", "tel"}:
            field_type = "phone"
        elif value_type in {"integer", "number"}:
            field_type = "number"
        elif value_type == "boolean":
            field_type = "boolean"
        elif value_type in {"array", "object", "json"}:
            field_type = "json"
        fields.append({
            "key": field_key,
            "label": re.sub(r"[_.-]+", " ", field_key).strip().title(),
            "required": required,
            "type": field_type,
        })
        seen_fields.add(field_key)

    def add_operation_fields(operation: dict) -> None:
        if not isinstance(operation, dict):
            return
        if str(operation.get("input_mode") or "").strip().lower() == "json_array":
            add_field("items", required=True, raw_type="array")
        for raw_key in operation.get("path_params") or []:
            add_field(raw_key, required=True)

        required_header_keys = {
            str(item or "").strip().lower()
            for item in (operation.get("required_header_params") or [])
            if str(item or "").strip()
        }
        declared_header_keys = {
            str(item or "").strip().lower()
            for item in (operation.get("header_params") or [])
            if str(item or "").strip()
        }
        for raw_key in operation.get("header_params") or []:
            add_field(
                raw_key,
                required=str(raw_key or "").strip().lower() in required_header_keys,
            )
        for raw_key in operation.get("required_header_params") or []:
            if str(raw_key or "").strip().lower() not in declared_header_keys:
                add_field(raw_key, required=True)

        required_query_keys = {
            str(item or "").strip()
            for item in (operation.get("required_query_params") or [])
            if str(item or "").strip()
        }
        declared_query_keys = {
            str(item or "").strip()
            for item in (operation.get("query_params") or [])
            if str(item or "").strip()
        }
        for raw_key in operation.get("query_params") or []:
            add_field(
                raw_key,
                required=str(raw_key or "").strip() in required_query_keys,
            )
        for raw_key in operation.get("required_query_params") or []:
            if str(raw_key or "").strip() not in declared_query_keys:
                add_field(raw_key, required=True)

        json_field_keys: set[str] = set()
        for raw_field in operation.get("json_fields") or []:
            if not isinstance(raw_field, dict):
                continue
            raw_key = str(raw_field.get("key") or "").strip()
            if not raw_key:
                continue
            json_field_keys.add(raw_key)
            add_field(
                raw_key,
                required=bool(raw_field.get("required")),
                raw_type=str(raw_field.get("type") or "string"),
                raw_format=str(raw_field.get("format") or ""),
            )
        for raw_key in operation.get("required_json_fields") or []:
            if str(raw_key or "").strip() not in json_field_keys:
                add_field(raw_key, required=True)

        form_field_keys: set[str] = set()
        for raw_field in operation.get("form_fields") or []:
            if not isinstance(raw_field, dict):
                continue
            raw_key = str(raw_field.get("key") or "").strip()
            if not raw_key:
                continue
            form_field_keys.add(raw_key)
            add_field(
                raw_key,
                required=bool(raw_field.get("required")),
                raw_type=str(raw_field.get("type") or "string"),
                raw_format=str(raw_field.get("format") or ""),
            )
        for raw_key in operation.get("required_form_fields") or []:
            if str(raw_key or "").strip() not in form_field_keys:
                add_field(raw_key, required=True)

    if execute_operation:
        add_operation_fields(execute_operation)

    if key == "booking":
        availability_operation = (
            operations.get("availability")
            if isinstance(operations.get("availability"), dict)
            else {}
        )
        if availability_operation:
            add_operation_fields(availability_operation)
            availability = {"mode": "integration"}

        # Packaged calendar connectors intentionally have no HTTP request schema.
        # Keep Xvond's semantic booking fields only for those schema-less
        # connectors; imported/custom booking APIs use their exact provider
        # contract instead of being forced into customer_name/date/time.
        if not fields:
            fields = [
                {"key": "customer_name", "label": "Customer name", "required": True, "type": "text"},
                {"key": "phone", "label": "Phone", "required": True, "type": "phone"},
                {"key": "service", "label": "Service", "required": True, "type": "text"},
                {"key": "date", "label": "Date", "required": True, "type": "date", "role": "date"},
                {"key": "time", "label": "Time", "required": True, "type": "time", "role": "time"},
                {"key": "notes", "label": "Notes", "required": False, "type": "text"},
            ]
            if availability_operation:
                availability.update({
                    "date_field": "date",
                    "time_field": "time",
                })
        elif availability_operation:
            date_field = next(
                (
                    field["key"]
                    for field in fields
                    if field.get("type") == "date"
                ),
                None,
            )
            time_field = next(
                (
                    field["key"]
                    for field in fields
                    if field.get("type") == "time"
                ),
                None,
            )
            if date_field:
                availability["date_field"] = date_field
            if time_field:
                availability["time_field"] = time_field

    module_map = {
        "booking": "booking",
        "orders": "orders",
        "lead_management": "lead_management",
        "quotation": "quotation",
        "customer_support": "customer_support",
    }
    return {
        "enabled": True,
        "_xvond_permission_mode": _permission_mode(spec, requirement),
        "label": purpose[:200] or key,
        "description": purpose[:1000],
        "module": module_map.get(key, "tools"),
        "fields": fields,
        "confirmation_required": _permission_mode(spec, requirement) != "automatic",
        "destination": {
            "type": "integration",
            "integration_id": integration_id,
            "operations": operations,
            **({"default_operation": default_operation} if default_operation else {}),
            "validation_required": requirement.get("validation_required") is True,
        },
        "availability": availability,
        "xvond_generated": True,
    }


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
    fulfillment_mode = str(requirement.get("fulfillment_mode") or "")
    if key == "booking" and fulfillment_mode == "xvond_internal":
        return build_internal_booking_action_config(requirement=requirement, spec=spec)
    if key in {"lead_management", "orders", "quotation", "customer_support"} and fulfillment_mode == "xvond_internal":
        return build_internal_record_action_config(requirement=requirement, spec=spec)
    if fulfillment_mode == "external_connection" and requirement.get("integration_id"):
        return build_external_integration_action_config(requirement=requirement, spec=spec)

    graph_backed = any(
        key in graph_action_types(routine.get("graph") or {})
        for routine in _compiled_execution_routines(spec)
        if isinstance(routine, dict)
    )
    legacy_plan = [
        dict(step)
        for step in (requirement.get("execution_plan") or [])
        if isinstance(step, dict)
    ][:20]

    return {
        "enabled": True,
        "_xvond_permission_mode": _permission_mode(spec, requirement),
        "label": purpose[:200] or key,
        "description": purpose[:1000],
        "module": "tools",
        "fields": [],
        "confirmation_required": _permission_mode(spec, requirement) != "automatic",
        "destination": {
            "type": "xvond_internal",
            "adapter": "generic_capability",
            "capability_key": key,
            "delivery_mode": "graph" if graph_backed else "legacy_plan",
            "graph_backed": graph_backed,
            "primitives": primitives,
            # execution_plan is retained only for pre-graph/tiny legacy contracts.
            # New universal employees execute their real work through the
            # compiled execution graph and use this action as a bounded side-effect
            # contract only when the graph explicitly references it.
            "execution_plan": legacy_plan,
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


BUSINESS_MODULE_KEYS = {
    "booking",
    "orders",
    "lead_management",
    "quotation",
    "customer_support",
}


def _enable_company_module(db, company_id: int, module_name: str) -> None:
    if module_name not in BUSINESS_MODULE_KEYS:
        return
    row = (
        db.query(CompanyModule)
        .filter(
            CompanyModule.company_id == company_id,
            CompanyModule.module_name == module_name,
        )
        .first()
    )
    if row is None:
        db.add(
            CompanyModule(
                company_id=company_id,
                module_name=module_name,
                enabled=True,
            )
        )
    else:
        row.enabled = True


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


MAX_EXECUTION_ROUTINES = 20


def _compiled_execution_routines(spec: dict) -> list[dict]:
    """Return the employee's independent executable routines.

    execution_routines is the multi-routine contract. The legacy execution_graph
    remains a supported single-routine contract and is exposed as routine
    "primary" so old employees keep working unchanged.
    """

    raw_routines = spec.get("execution_routines")
    result: list[dict] = []
    used_ids: set[str] = set()

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

            base_id = normalize_requirement_key(
                raw.get("id") or raw.get("key") or raw.get("name") or f"routine_{index + 1}"
            )[:80] or f"routine_{index + 1}"
            routine_id = base_id
            suffix = 2
            while routine_id in used_ids:
                routine_id = f"{base_id[:70]}_{suffix}"
                suffix += 1
            used_ids.add(routine_id)

            name = str(raw.get("name") or routine_id.replace("_", " ")).strip()[:200]
            requirement_keys: list[str] = []
            for raw_key in raw.get("requirement_keys") or []:
                key = normalize_requirement_key(raw_key)
                if key and key not in requirement_keys:
                    requirement_keys.append(key)

            result.append(
                {
                    "id": routine_id,
                    "name": name or routine_id,
                    "requirement_keys": requirement_keys,
                    "requirement_scope_declared": "requirement_keys" in raw,
                    "graph": graph,
                }
            )
            if len(result) >= MAX_EXECUTION_ROUTINES:
                break

    if result:
        return result

    legacy_graph = normalize_execution_graph(spec.get("execution_graph") or {})
    if legacy_graph.get("nodes"):
        return [
            {
                "id": "primary",
                "name": "Primary routine",
                "requirement_keys": [],
                "requirement_scope_declared": False,
                "graph": legacy_graph,
            }
        ]
    return []


def _routine_graph_for_action(routines: list[dict], action_type: str) -> dict | None:
    target = str(action_type or "").strip()
    for routine in routines:
        graph = routine.get("graph")
        if isinstance(graph, dict) and target in graph_action_types(graph):
            return graph
    return None


def _generated_graph_trigger_workflow(
    db,
    *,
    company_id: int,
    agent_id: int,
    trigger_type: str,
    routine_id: str = "primary",
) -> AutomationWorkflow | None:
    rows = (
        db.query(AutomationWorkflow)
        .filter(
            AutomationWorkflow.company_id == company_id,
            AutomationWorkflow.trigger_type == trigger_type,
        )
        .all()
    )
    for row in rows:
        config = row.trigger_config if isinstance(row.trigger_config, dict) else {}
        stored_routine_id = str(config.get("_xvond_routine_id") or "").strip()
        routine_matches = (
            stored_routine_id == routine_id
            or (routine_id == "primary" and not stored_routine_id)
        )
        if (
            config.get("_xvond_source") == "self_service_employee"
            and int(config.get("_xvond_agent_id") or 0) == int(agent_id)
            and config.get("_xvond_graph_trigger") is True
            and routine_matches
        ):
            return row
    return None


def _provision_self_service_graph_trigger(
    db,
    *,
    company: Company,
    timezone: str | None,
    agent_id: int,
    execution_graph: dict | None,
    actions: dict,
    action_plan: dict,
    runtime_inputs: dict | None = None,
    routine_id: str = "primary",
    routine_name: str | None = None,
    requirement_keys: list[str] | None = None,
) -> tuple[str, int | None]:
    if str(company.onboarding_source or "").strip().lower() != "self_service":
        return "managed_delivery", None

    graph = normalize_execution_graph(execution_graph or {})
    if not graph.get("nodes"):
        return "not_required", None
    if graph_contract_errors(graph, graph_agent_id=agent_id):
        return "setup_required", None
    trigger = graph.get("trigger") or {"type": "manual"}
    trigger_type = str(trigger.get("type") or "manual").strip().lower()
    if trigger_type not in {"manual", "schedule", "webhook", "event"}:
        return "not_required", None
    normalized_graph_schedule = None
    if trigger_type == "schedule":
        raw_schedule = trigger.get("schedule")
        if not isinstance(raw_schedule, dict):
            return "schedule_required", None
        try:
            normalized_graph_schedule = normalize_schedule_config(
                raw_schedule,
                default_timezone=timezone,
            )
        except ScheduleConfigError:
            return "schedule_setup_required", None
    if trigger_type == "event" and not str(trigger.get("event") or "").strip():
        return "setup_required", None

    action_types = graph_action_types(graph)
    for action_type in action_types:
        action = actions.get(action_type)
        plan = action_plan.get(action_type) or {}
        if not isinstance(action, dict):
            return "setup_required", None
        if str(action.get("_xvond_permission_mode") or "").strip().lower() == "never":
            # An owner-denied node is a deliberate no-op at runtime, not missing
            # setup. Other graph work may still run normally.
            continue
        if str(plan.get("execution_status") or "") != "ready":
            return "setup_required", None

    workflow = _generated_graph_trigger_workflow(
        db,
        company_id=company.id,
        agent_id=agent_id,
        trigger_type=trigger_type,
        routine_id=routine_id,
    )
    if workflow is not None and not workflow.enabled:
        return "disabled", workflow.id
    if workflow is None:
        workflow = AutomationWorkflow(
            company_id=company.id,
            name=(routine_name or f"AI Employee {trigger_type} routine")[:200],
            trigger_type=trigger_type,
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": agent_id,
                "_xvond_graph_trigger": True,
                "_xvond_routine_id": routine_id,
                "_xvond_routine_name": (routine_name or routine_id)[:200],
                "_xvond_requirement_keys": list(requirement_keys or []),
                "_xvond_runtime_inputs": dict(runtime_inputs or {}),
                "_xvond_generated": True,
                **(
                    {"event_name": str(trigger.get("event") or "").strip()[:120]}
                    if trigger_type == "event"
                    else {}
                ),
                **(
                    {
                        "schedule": normalized_graph_schedule,
                        "input_data": dict(runtime_inputs or {}),
                    }
                    if trigger_type == "schedule"
                    else {}
                ),
            },
            steps=[
                {
                    "type": "graph",
                    "agent_id": agent_id,
                    "graph": graph,
                }
            ],
            enabled=True,
        )
        db.add(workflow)
        db.flush()
    return "ready", workflow.id


def _provision_self_service_schedule(
    db,
    *,
    company: Company,
    timezone: str | None,
    agent_id: int,
    requirement: dict,
    action: dict,
    execution_graph: dict | None = None,
) -> tuple[str, int | None]:
    if str(company.onboarding_source or "").strip().lower() != "self_service":
        return "managed_delivery", None
    if "scheduler" not in (requirement.get("primitives") or []):
        return "not_required", None
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

    if str(action.get("_xvond_permission_mode") or "").strip().lower() == "never":
        return "permission_denied", None
    if action.get("confirmation_required", True):
        return "approval_required", None

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
        task_purpose = (
            str(requirement.get("purpose") or key.replace("_", " ")).strip()
            or "Scheduled employee task"
        )
        compiled_graph = normalize_execution_graph(execution_graph or {})
        graph_nodes = list(compiled_graph.get("nodes") or [])
        target_ids = {
            str(node.get("id") or "")
            for node in graph_nodes
            if isinstance(node, dict)
            and node.get("type") == "action"
            and str((node.get("params") or {}).get("action_type") or "") == key
        }
        if target_ids:
            by_id = {
                str(node.get("id") or ""): node
                for node in graph_nodes
                if isinstance(node, dict)
            }
            keep = set(target_ids)
            changed_deps = True
            while changed_deps:
                changed_deps = False
                for node_id in list(keep):
                    node = by_id.get(node_id) or {}
                    for dep in node.get("depends_on") or []:
                        if dep not in keep and dep in by_id:
                            keep.add(dep)
                            changed_deps = True
            selected_graph = {
                "version": compiled_graph.get("version") or 1,
                "nodes": [
                    node for node in graph_nodes
                    if str(node.get("id") or "") in keep
                ],
            }
        else:
            fallback_nodes: list[dict] = []
            previous_id = None
            if "content_generation" in (requirement.get("primitives") or []):
                previous_id = "generate_content"
                fallback_nodes.append(
                    {
                        "id": previous_id,
                        "type": "ai",
                        "depends_on": [],
                        "params": {
                            "prompt": (
                                "Perform this scheduled employee task now. "
                                f"Task: {task_purpose}. "
                                "Use the employee's current instructions and knowledge. "
                                "Return the final content/result for the next step."
                            )[:2000],
                        },
                    }
                )
            if "media_generation" in (requirement.get("primitives") or []):
                deps = [previous_id] if previous_id else []
                previous_id = "generate_media"
                fallback_nodes.append(
                    {
                        "id": previous_id,
                        "type": "media",
                        "depends_on": deps,
                        "params": {
                            "prompt": (
                                "Create the publishable visual for this task."
                            ),
                            "size": "1024x1024",
                        },
                    }
                )
            action_args = {}
            if any(node.get("id") == "generate_content" for node in fallback_nodes):
                action_args["caption"] = "$nodes.generate_content.ai_response"
            if any(node.get("id") == "generate_media" for node in fallback_nodes):
                action_args["media_url"] = "$nodes.generate_media.media_url"
            fallback_nodes.append(
                {
                    "id": "execute_action",
                    "type": "action",
                    "depends_on": [previous_id] if previous_id else [],
                    "params": {
                        "action_type": key,
                        "arguments": action_args,
                    },
                }
            )
            selected_graph = {"version": 1, "nodes": fallback_nodes}

        if graph_contract_errors(selected_graph, graph_agent_id=agent_id):
            return "setup_required", None

        workflow_steps = [
            {
                "type": "graph",
                "agent_id": agent_id,
                "graph": selected_graph,
            }
        ]
        workflow = AutomationWorkflow(
            company_id=company.id,
            name=task_purpose[:200],
            trigger_type="schedule",
            trigger_config={
                "_xvond_source": "self_service_employee",
                "_xvond_agent_id": agent_id,
                "_xvond_requirement_key": key,
                "_xvond_generated": True,
                "schedule": schedule,
                "input_data": runtime_inputs,
            },
            steps=workflow_steps,
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
    execution_routines = _compiled_execution_routines(prepared)
    scheduled_graph_action_types: set[str] = set()
    for routine in execution_routines:
        graph = routine.get("graph") if isinstance(routine, dict) else None
        if not isinstance(graph, dict):
            continue
        trigger_type = str(
            (graph.get("trigger") or {}).get("type") or "manual"
        ).strip().lower()
        if trigger_type == "schedule":
            scheduled_graph_action_types.update(graph_action_types(graph))
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
        # Existing operator configuration and destination setup win, but
        # Xvond-generated policy flags must follow the owner's latest permission.
        action = actions[key]
        if action.get("xvond_generated") is True:
            permission_mode = _permission_mode(prepared, item)
            desired_confirmation = permission_mode != "automatic"
            if (
                action.get("confirmation_required", True) != desired_confirmation
                or action.get("_xvond_permission_mode") != permission_mode
            ):
                # Operator enable/disable is a separate, higher-priority switch.
                # Owner policy controls whether an enabled generated action may
                # execute automatically, require approval, or be denied.
                action["confirmation_required"] = desired_confirmation
                action["_xvond_permission_mode"] = permission_mode
                actions[key] = action
                changed = True
        destination = action.get("destination") or {}
        permission_mode = str(action.get("_xvond_permission_mode") or "").strip().lower()
        if permission_mode == "never":
            execution_status = "permission_denied"
        elif not action.get("enabled", True) or (assignment is not None and not assignment.enabled):
            execution_status = "disabled"
        elif destination.get("type") == "xvond_internal" and destination.get("adapter") == "booking":
            execution_status = (
                "ready"
                if action.get("_xvond_booking_setup_ready") is True
                else "setup_required"
            )
        elif destination.get("type") == "integration":
            integration_id = destination.get("integration_id")
            integration = None
            if company is not None and integration_id:
                integration = (
                    db.query(CompanyIntegration)
                    .filter(
                        CompanyIntegration.id == int(integration_id),
                        CompanyIntegration.company_id == company.id,
                        CompanyIntegration.enabled.is_(True),
                    )
                    .first()
                )
            validation_ready = bool(
                integration is not None
                and (
                    destination.get("validation_required") is not True
                    or integration_validation_ready(
                        reveal_config(integration.config) or {}
                    )
                )
            )
            execution_status = "ready" if validation_ready else "setup_required"
        elif destination.get("type") == "xvond_internal" and destination.get("adapter") == "business_record":
            execution_status = "ready"
        elif destination.get("type") == "xvond_internal" and destination.get("adapter") == "generic_capability":
            # A graph action is still a real side effect. Merely referencing a
            # capability from the graph does not create an executor for it.
            # Only mark the action ready when its bounded internal execution
            # contract is actually runnable. Novel graph-native work should use
            # native graph nodes; external side effects should bind a connection.
            execution_status = (
                "ready"
                if generic_capability_readiness(action).get("ready") is True
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
            and key not in scheduled_graph_action_types
        ):
            schedule_status, schedule_workflow_id = _provision_self_service_schedule(
                db,
                company=company,
                timezone=company_timezone,
                agent_id=agent_id,
                requirement=item,
                action=action,
                execution_graph=_routine_graph_for_action(execution_routines, key)
                or prepared.get("execution_graph"),
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
        if company is not None:
            module_name = str(action.get("module") or "").strip()
            _enable_company_module(db, company.id, module_name)

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

    requirements_by_key = {
        normalize_requirement_key(item.get("key")): item
        for item in requirements
        if isinstance(item, dict) and normalize_requirement_key(item.get("key"))
    }
    shared_graph_runtime_inputs: dict = {}
    for requirement in requirements:
        if isinstance(requirement, dict):
            for key, value in (requirement.get("runtime_inputs") or {}).items():
                shared_graph_runtime_inputs.setdefault(str(key), value)

    graph_triggers: list[dict] = []
    if company is not None:
        for routine in execution_routines:
            routine_id = str(routine.get("id") or "primary").strip() or "primary"
            routine_name = str(routine.get("name") or routine_id).strip() or routine_id
            graph = routine.get("graph") if isinstance(routine.get("graph"), dict) else {}
            requirement_keys = [
                key
                for key in (
                    normalize_requirement_key(item)
                    for item in (routine.get("requirement_keys") or [])
                )
                if key and key in requirements_by_key
            ]

            runtime_input_conflicts: list[str] = []
            if requirement_keys:
                routine_runtime_inputs: dict = {}
                for requirement_key in requirement_keys:
                    requirement = requirements_by_key[requirement_key]
                    for raw_key, value in (requirement.get("runtime_inputs") or {}).items():
                        key = str(raw_key)
                        if (
                            key in routine_runtime_inputs
                            and routine_runtime_inputs[key] != value
                        ):
                            if key not in runtime_input_conflicts:
                                runtime_input_conflicts.append(key)
                            continue
                        routine_runtime_inputs.setdefault(key, value)
            elif routine.get("requirement_scope_declared") is True:
                # v10+ explicit empty scope means this routine intentionally
                # consumes no requirement-owned runtime defaults.
                routine_runtime_inputs = {}
            else:
                # Backward compatibility only for older compiled employees
                # whose routines never had a requirement scope field.
                routine_runtime_inputs = dict(shared_graph_runtime_inputs)

            if runtime_input_conflicts:
                status, workflow_id = "runtime_input_conflict", None
            else:
                status, workflow_id = _provision_self_service_graph_trigger(
                    db,
                    company=company,
                    timezone=company_timezone,
                    agent_id=agent_id,
                    execution_graph=graph,
                    actions=actions,
                    action_plan=action_plan,
                    runtime_inputs=routine_runtime_inputs,
                    routine_id=routine_id,
                    routine_name=routine_name,
                    requirement_keys=requirement_keys,
                )
            graph_triggers.append(
                {
                    "routine_id": routine_id,
                    "routine_name": routine_name,
                    "requirement_keys": requirement_keys,
                    "runtime_input_conflicts": runtime_input_conflicts,
                    "status": status,
                    "workflow_id": workflow_id,
                    "trigger_type": str(
                        (graph.get("trigger") or {}).get("type") or "manual"
                    ),
                }
            )

    if graph_triggers:
        primary_graph_trigger = dict(graph_triggers[0])
    else:
        primary_graph_trigger = {
            "routine_id": "primary",
            "routine_name": "Primary routine",
            "status": "not_required",
            "workflow_id": None,
            "trigger_type": "manual",
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
        # Keep the singular field for old portal/API consumers.
        "graph_trigger": primary_graph_trigger,
        "graph_triggers": graph_triggers,
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
