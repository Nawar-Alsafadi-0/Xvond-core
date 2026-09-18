from copy import deepcopy

from backend.app.core.config_secrets import reveal_config
from backend.app.core.error_safety import safe_error_label
from backend.app.core.module_access import require_company_module
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent.models import AIAgent, AIConversation
from backend.app.modules.tools.business_models import ActionRequest
from backend.app.modules.tools.models import AgentToolAssignment, ToolApprovalRequest
from backend.app.modules.tools.registry import tool_registry
from backend.app.modules.tools.bootstrap import register_builtin_tools
from backend.app.modules.tools.generic_capability_runtime import generic_capability_readiness

SENSITIVE_TOOLS = {"webhook", "custom_api"}
LEGACY_BUSINESS_TOOLS = {"booking", "order", "lead"}


def tool_requires_approval(tool_name: str, config: dict | None) -> bool:
    config = config or {}
    if tool_name in SENSITIVE_TOOLS:
        return True
    return bool(config.get("approval_required", False))


def _enabled_actions(config: dict | None) -> dict:
    actions = (config or {}).get("actions") or {}
    if not isinstance(actions, dict):
        return {}
    return {
        key: value
        for key, value in actions.items()
        if isinstance(value, dict) and value.get("enabled", True)
    }


def _operation_config_ready(action: dict) -> bool:
    destination = action.get("destination") or {}
    destination_type = str(destination.get("type") or "unconfigured").strip()
    if destination_type == "xvond_internal" and destination.get("adapter") == "generic_capability":
        return bool(generic_capability_readiness(action).get("ready"))
    if destination_type in {"unconfigured", "workflow_engine"}:
        # Generated workflow contracts still need an actual execution adapter.
        return False
    if destination_type == "integration" and not destination.get("integration_id"):
        return False

    availability = action.get("availability") or {}
    mode = str(availability.get("mode") or "none").strip()
    if mode == "xvond_schedule":
        schedule = availability.get("schedule") or {}
        if not availability.get("date_field") or not availability.get("time_field"):
            return False
        if not schedule.get("weekdays") or not schedule.get("start") or not schedule.get("end"):
            return False
    if mode == "integration" and destination_type != "integration":
        return False
    return True


def _field_list(action: dict) -> list[str]:
    raw = action.get("fields") or action.get("required_fields") or []
    result = []
    for item in raw:
        if isinstance(item, str):
            key = item.strip()
            if key:
                result.append(key)
        elif isinstance(item, dict):
            key = str(item.get("key") or item.get("name") or "").strip()
            if key and item.get("required", True):
                label = str(item.get("label") or key).strip()
                result.append(f"{key} ({label})")
    return result


def _runtime_description(tool, config: dict) -> str:
    description = tool.description
    config = config or {}
    if tool.name == "action_request":
        rules = []
        for action_type, action in _enabled_actions(config).items():
            fields = _field_list(action)
            destination = str((action.get("destination") or {}).get("type") or "unconfigured")
            availability = str((action.get("availability") or {}).get("mode") or "none")
            confirmation = "required" if action.get("confirmation_required", True) else "not required"
            module = str(action.get("module") or "unassigned")
            rules.append(
                f"ACTION {action_type} ({action.get('label') or action_type}): "
                f"module={module}; purpose={action.get('description') or 'configured business operation'}; "
                f"required details={', '.join(fields) if fields else 'none'}; "
                f"destination={destination}; availability={availability}; customer confirmation={confirmation}."
            )
        if rules:
            description += " CURRENT CONFIGURED BUSINESS ACTIONS: " + " ".join(rules)
            description += (
                " These current actions are authoritative and override any older booking/order/lead mode wording that may exist in the employee prompt."
                " Understand the customer's intent and use the matching configured action only."
                " Collect only missing required details progressively and remember details already supplied in conversation history."
                " If availability is configured, use check_availability before promising a time."
                " When details are complete, call prepare. If confirmation is required, present the returned summary and ask for explicit confirmation."
                " On the customer's next confirming message, call execute for the same action; Xvond will resolve the pending request automatically, so never ask the customer for an internal request ID."
                " Never claim success until execute returns success."
                " Never hand off a configured operation unless that operation's configured destination is human_handoff."
                " If a destination is unconfigured or its capability module is disabled, explain that the operation cannot currently be completed instead of pretending it succeeded."
            )
    if tool.name == "lead":
        fields = [
            str(x).strip()
            for x in (config.get("required_fields") or [])
            if str(x).strip()
        ]
        if fields:
            description += (
                " Required lead details: "
                + ", ".join(fields)
                + ". Collect missing details naturally before saving."
            )
    return description


def _runtime_schema(tool, config: dict) -> dict:
    schema = deepcopy(tool.input_schema)
    if tool.name == "action_request":
        enabled = list(_enabled_actions(config).keys())
        if enabled:
            schema.setdefault("properties", {}).setdefault("action_type", {})[
                "enum"
            ] = enabled
    return schema


def _pending_request(
    db,
    company_id: int,
    agent_id: int,
    conversation_id: int | None,
    action_type: str,
    operation: str,
):
    if conversation_id is None:
        return None
    query = db.query(ActionRequest).filter(
        ActionRequest.company_id == company_id,
        ActionRequest.agent_id == agent_id,
        ActionRequest.conversation_id == conversation_id,
        ActionRequest.action_type == action_type,
    )
    if operation == "execute":
        # Include durable post-execution/unresolved states. A retried AI turn must
        # resolve the same request so the action tool can return already_executed
        # or reconciliation-required instead of creating/replaying a side effect.
        query = query.filter(
            ActionRequest.status.in_(
                [
                    "awaiting_confirmation",
                    "new",
                    "confirmed",
                    "executing",
                    "external_failed",
                ]
            )
        )
    elif operation == "cancel":
        query = query.filter(
            ActionRequest.status.notin_(["completed", "cancelled"])
        )
    return query.order_by(ActionRequest.id.desc()).first()


def _action_module_enabled(db, company_id: int, config: dict, action_type: str) -> bool:
    action = ((config or {}).get("actions") or {}).get(action_type)
    if not isinstance(action, dict) or not action.get("enabled", True):
        return False
    module_name = str(action.get("module") or "").strip()
    if not module_name:
        return False
    row = (
        db.query(CompanyModule)
        .filter(
            CompanyModule.company_id == company_id,
            CompanyModule.module_name == module_name,
            CompanyModule.enabled.is_(True),
        )
        .first()
    )
    return row is not None


def _action_runtime_ready(db, company_id: int, config: dict, action_type: str) -> bool:
    action = ((config or {}).get("actions") or {}).get(action_type)
    return bool(
        isinstance(action, dict)
        and action.get("enabled", True)
        and _operation_config_ready(action)
        and _action_module_enabled(db, company_id, config, action_type)
    )


class ToolExecutor:
    def __init__(self):
        register_builtin_tools()

    def get_agent_tools(self, db, agent_id: int) -> list[dict]:
        assignments = (
            db.query(AgentToolAssignment)
            .filter(
                AgentToolAssignment.agent_id == agent_id,
                AgentToolAssignment.enabled.is_(True),
            )
            .order_by(AgentToolAssignment.id.asc())
            .all()
        )
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        company_id = agent.company_id if agent else None
        generic_config = {}
        for assignment in assignments:
            if assignment.tool_name == "action_request":
                generic_config = reveal_config(assignment.config) or {}
                break

        generic_actions = _enabled_actions(generic_config)
        if company_id is not None:
            generic_actions = {
                key: action
                for key, action in generic_actions.items()
                if _action_runtime_ready(db, company_id, generic_config, key)
            }
            if generic_config.get("actions"):
                generic_config = dict(generic_config)
                generic_config["actions"] = generic_actions

        generic_active = bool(generic_actions)
        allows_handoff = any(
            str((action.get("destination") or {}).get("type") or "")
            == "human_handoff"
            for action in generic_actions.values()
        )

        result = []
        for assignment in assignments:
            if generic_active and assignment.tool_name in LEGACY_BUSINESS_TOOLS:
                continue
            if (
                generic_active
                and assignment.tool_name == "human_handoff"
                and not allows_handoff
            ):
                continue
            tool = tool_registry.get(assignment.tool_name)
            if tool is not None:
                config = reveal_config(assignment.config) or {}
                if assignment.tool_name == "action_request":
                    config = generic_config
                    if not generic_actions:
                        continue
                result.append(
                    {
                        "name": tool.name,
                        "description": _runtime_description(tool, config),
                        "input_schema": _runtime_schema(tool, config),
                    }
                )
        return result

    def validate_execution_scope(
        self,
        db,
        company_id: int,
        agent_id: int,
        conversation_id: int | None = None,
    ) -> str | None:
        agent = (
            db.query(AIAgent)
            .filter(
                AIAgent.id == agent_id,
                AIAgent.company_id == company_id,
            )
            .first()
        )
        if agent is None:
            return "Agent does not belong to this company"
        if conversation_id is None:
            return None
        conversation = (
            db.query(AIConversation)
            .filter(
                AIConversation.id == conversation_id,
                AIConversation.company_id == company_id,
                AIConversation.agent_id == agent_id,
            )
            .first()
        )
        return (
            None
            if conversation is not None
            else "Conversation does not belong to this company and agent"
        )

    def execute(
        self,
        db,
        company_id: int,
        agent_id: int,
        tool_name: str,
        arguments: dict,
        conversation_id: int | None = None,
        approval_granted: bool = False,
    ) -> dict:
        scope_error = self.validate_execution_scope(
            db,
            company_id,
            agent_id,
            conversation_id,
        )
        if scope_error is not None:
            return {
                "success": False,
                "tool": tool_name,
                "data": None,
                "error": scope_error,
            }
        require_company_module(db, company_id, "tools")
        assignment = (
            db.query(AgentToolAssignment)
            .filter(
                AgentToolAssignment.agent_id == agent_id,
                AgentToolAssignment.tool_name == tool_name,
                AgentToolAssignment.enabled.is_(True),
            )
            .first()
        )
        if assignment is None:
            return {
                "success": False,
                "tool": tool_name,
                "data": None,
                "error": "Tool is not assigned to this agent",
            }
        tool = tool_registry.get(tool_name)
        if tool is None:
            return {
                "success": False,
                "tool": tool_name,
                "data": None,
                "error": "Tool is not registered",
            }
        config = reveal_config(assignment.config) or {}

        actual_arguments = dict(arguments or {})
        if tool_name == "action_request":
            action_type = str(actual_arguments.get("action_type") or "").strip()
            if not action_type or not _action_runtime_ready(
                db,
                company_id,
                config,
                action_type,
            ):
                return {
                    "success": False,
                    "tool": tool_name,
                    "data": None,
                    "error": "This business operation is not fully configured and active for the company",
                }

        approval_required = tool_requires_approval(tool_name, config)
        if approval_required and not approval_granted:
            request = ToolApprovalRequest(
                company_id=company_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                arguments=actual_arguments,
                status="pending",
            )
            db.add(request)
            db.flush()
            return {
                "success": False,
                "tool": tool_name,
                "data": {
                    "approval_request_id": request.id,
                    "status": "pending_approval",
                },
                "error": "Tool execution requires human approval",
                "approval_required": True,
            }

        if tool_name == "action_request" and not actual_arguments.get("request_id"):
            operation = str(actual_arguments.get("operation") or "").strip()
            action_type = str(actual_arguments.get("action_type") or "").strip()
            if operation in {"execute", "cancel", "status"} and action_type:
                pending = _pending_request(
                    db,
                    company_id,
                    agent_id,
                    conversation_id,
                    action_type,
                    operation,
                )
                if pending is not None:
                    actual_arguments["request_id"] = pending.id

        context = {
            "db": db,
            "company_id": company_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "config": config,
        }
        try:
            with db.begin_nested():
                result = tool.execute(
                    arguments=actual_arguments,
                    context=context,
                )
                db.flush()
            return {
                "success": bool(result.success),
                "tool": tool_name,
                "data": result.data,
                "error": result.error,
            }
        except Exception as exc:
            return {
                "success": False,
                "tool": tool_name,
                "data": None,
                "error": safe_error_label(exc),
            }


tool_executor = ToolExecutor()
