from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter

from sqlalchemy.exc import IntegrityError

from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.config_secrets import reveal_config
from backend.app.core.http_security import safe_http_request
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.agent_state import (
    delete_agent_state,
    read_agent_state,
    write_agent_state,
)
from backend.app.modules.automation.browser_runtime import (
    BrowserExecutionError,
    run_browser_task,
)
from backend.app.modules.automation.execution_graph import (
    compare_values,
    extract_data_path,
    normalize_execution_graph,
    resolve_graph_value,
)
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.customer_ops.models import NotificationEvent
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.media.generated_media import generate_image_asset
from backend.app.modules.tools.executor import tool_executor
from backend.app.modules.tools.action_request import _integration_call
from backend.app.modules.tools.generic_capability_runtime import execute_generic_capability
from backend.app.modules.tools.models import AgentToolAssignment
from backend.app.modules.tools.business_models import ActionRequest


def _utcnow_naive() -> datetime:
    """Return UTC without tzinfo for compatibility with existing naive DB timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


MAX_AI_STEP_MESSAGE_CHARS = 12000
MAX_NESTED_GRAPH_DEPTH = 2


def _render_step_context(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    import json
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise ValueError("AI/media step context must be JSON serializable") from exc


def _compose_step_message(*, prompt: str, context=None) -> str:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("AI/media step requires prompt")
    if len(clean_prompt) > MAX_AI_STEP_MESSAGE_CHARS:
        raise ValueError("AI/media step prompt is too long")
    context_text = _render_step_context(context)
    if not context_text:
        return clean_prompt

    separator = "\n\nCONTEXT:\n"
    remaining = MAX_AI_STEP_MESSAGE_CHARS - len(clean_prompt) - len(separator)
    if remaining <= 0:
        return clean_prompt
    if len(context_text) > remaining:
        marker = "\n...[context truncated by Xvond]"
        keep = max(0, remaining - len(marker))
        context_text = context_text[:keep] + marker
    return clean_prompt + separator + context_text


def _billing_contract(workflow: AutomationWorkflow) -> tuple[str, str]:
    source = str((workflow.trigger_config or {}).get("_xvond_source") or "").strip()
    if source == "self_service_employee":
        return "ai_agents", "automation_runs"
    return "automation", "runs"


def _trace_id(*, company_id: int, workflow_id: int, execution_key: str) -> str:
    digest = sha256(
        f"{int(company_id)}:{int(workflow_id)}:{execution_key}".encode("utf-8")
    ).hexdigest()[:24]
    return f"xvond_trace_{digest}"


def _trace_iso(value: datetime | None = None) -> str:
    current = value or _utcnow_naive()
    return current.isoformat(timespec="milliseconds") + "Z"


def _new_trace(
    *,
    company_id: int,
    workflow: AutomationWorkflow,
    execution_key: str,
) -> dict:
    return {
        "version": 1,
        "trace_id": _trace_id(
            company_id=company_id,
            workflow_id=workflow.id,
            execution_key=execution_key,
        ),
        "workflow_id": workflow.id,
        "trigger_type": str(workflow.trigger_type or ""),
        "execution_key": execution_key,
        "started_at": _trace_iso(),
        "finished_at": None,
        "status": "running",
        "spans": [],
    }


def _append_step_span(
    trace: dict,
    *,
    index: int,
    step: dict,
    started_at: str,
    started_perf: float,
    status: str,
    phase: str = "execute",
    node_id: str | None = None,
    error: str | None = None,
) -> None:
    spans = trace.setdefault("spans", [])
    spans.append(
        {
            "span_id": f"step-{int(index)}-{len(spans) + 1}",
            "kind": "workflow_step",
            "step_index": int(index),
            "step_type": str(step.get("type") or ""),
            "label": step.get("label"),
            "phase": phase,
            "status": status,
            "node_id": str(node_id or "") or None,
            "started_at": started_at,
            "finished_at": _trace_iso(),
            "duration_ms": round(max(0.0, perf_counter() - started_perf) * 1000, 2),
            "error": str(error or "")[:2000] or None,
        }
    )


def _employee_notification_event(
    db,
    *,
    company_id: int,
    agent_id: int | None,
    run_id: int,
    execution_key: str,
    node_scope: str,
    title: str,
    message: str,
    severity: str = "info",
) -> tuple[NotificationEvent, bool]:
    """Persist one owner-visible notification per execution/node scope."""

    stable_execution = str(execution_key or f"run:{int(run_id)}").strip()
    stable_scope = str(node_scope or "notify").strip()
    digest = sha256(
        f"{int(company_id)}:{stable_execution}:{stable_scope}".encode("utf-8")
    ).hexdigest()
    event_key = f"employee-runtime:{digest}"

    existing = (
        db.query(NotificationEvent)
        .filter(
            NotificationEvent.company_id == int(company_id),
            NotificationEvent.event_key == event_key,
        )
        .first()
    )
    if existing is not None:
        return existing, True

    safe_severity = str(severity or "info").strip().lower()
    if safe_severity not in {"info", "warning", "critical"}:
        safe_severity = "info"

    event = NotificationEvent(
        company_id=int(company_id),
        event_key=event_key,
        event_type="employee_update",
        severity=safe_severity,
        title=str(title or "Employee update").strip()[:255] or "Employee update",
        message=str(message or "").strip()[:4000] or None,
        payload={
            "agent_id": int(agent_id) if agent_id else None,
            "automation_run_id": int(run_id),
            "node_scope": stable_scope,
            "execution_key": stable_execution[:500],
        },
        read=False,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        return event, False
    except IntegrityError:
        existing = (
            db.query(NotificationEvent)
            .filter(
                NotificationEvent.company_id == int(company_id),
                NotificationEvent.event_key == event_key,
            )
            .first()
        )
        if existing is None:
            raise
        return existing, True


def _checkpoint_fingerprint(value) -> str:
    import json

    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Automation checkpoint data is not serializable") from exc
    return sha256(payload.encode("utf-8")).hexdigest()


def _approval_checkpoint_matches(
    request: ActionRequest,
    *,
    node_id: str,
    approval_scope: str,
) -> bool:
    details = request.details if isinstance(request.details, dict) else {}
    meta = details.get("_xvond_automation")
    if not isinstance(meta, dict):
        return False

    expected_node = str(node_id or "").strip()
    expected_scope = str(approval_scope or expected_node).strip()
    recorded_scope = str(meta.get("approval_scope") or "").strip()
    if expected_scope:
        if recorded_scope:
            return recorded_scope == expected_scope
        # Backward compatibility is safe only for old top-level checkpoints.
        if expected_scope != expected_node:
            return False
    return str(meta.get("node_id") or "").strip() == expected_node


RETRY_CHECKPOINT_VERSION = 1
_RETRY_TRANSIENT_STATE_KEYS = {
    "_xvond_graph_resume",
    "_xvond_retry_root_step_index",
    "_xvond_retry_root_state",
    "_xvond_retry_lineage",
    "_xvond_loop_item",
    "_xvond_loop_index",
    "_xvond_nested_graph_depth",
    "_xvond_graph_path",
    "_xvond_approved_request_id",
}


def _retry_root_state(state: dict) -> dict:
    return {
        str(key): deepcopy(value)
        for key, value in dict(state or {}).items()
        if str(key) not in _RETRY_TRANSIENT_STATE_KEYS
    }


def _graph_retry_safety(
    db,
    *,
    graph_agent_id: int | None,
    node_type: str,
    params: dict,
    resolved: bool,
) -> tuple[bool, str | None]:
    clean_type = str(node_type or "").strip().lower()
    values = params if isinstance(params, dict) else {}

    if not resolved and clean_type in {"action", "browser", "media"}:
        return (
            False,
            "Retry safety could not be proven before this side-effect-capable node started.",
        )

    if clean_type == "media":
        return (
            False,
            "Generated media does not have a durable idempotency contract, so Xvond will not regenerate it automatically after an uncertain failure.",
        )

    if clean_type == "browser":
        actions = values.get("actions")
        mutating_ops = {"click", "fill", "press", "select"}
        if isinstance(actions, list) and any(
            isinstance(item, dict)
            and str(item.get("op") or "").strip().lower() in mutating_ops
            for item in actions
        ):
            return (
                False,
                "Interactive browser work may already have changed an external system; reconcile it before retrying.",
            )
        return True, None

    if clean_type == "action":
        agent_id = int(values.get("agent_id") or graph_agent_id or 0)
        action_type = str(values.get("action_type") or "").strip()
        if not agent_id or not action_type:
            return False, "The action identity is incomplete, so retry safety cannot be proven."

        assignment = (
            db.query(AgentToolAssignment)
            .filter(
                AgentToolAssignment.agent_id == agent_id,
                AgentToolAssignment.tool_name == "action_request",
                AgentToolAssignment.enabled.is_(True),
            )
            .first()
        )
        if assignment is None:
            return False, "The employee action contract is unavailable."

        config = reveal_config(assignment.config) or {}
        action = (config.get("actions") or {}).get(action_type)
        destination = action.get("destination") if isinstance(action, dict) else None
        destination = destination if isinstance(destination, dict) else {}

        if (
            destination.get("type") == "xvond_internal"
            and destination.get("adapter") == "generic_capability"
        ):
            return True, None

        return (
            False,
            "This action targets an external integration whose final outcome may be uncertain; reconcile the external system before retrying.",
        )

    return True, None


def _compose_retry_graph_resume(
    *,
    lineage: list,
    node_id: str,
    node_outputs: dict,
) -> dict:
    checkpoint = {
        "node_id": str(node_id or ""),
        "node_outputs": deepcopy(dict(node_outputs or {})),
    }
    for raw_frame in reversed(list(lineage or [])):
        frame = raw_frame if isinstance(raw_frame, dict) else {}
        checkpoint = {
            "node_id": str(frame.get("node_id") or ""),
            "node_outputs": deepcopy(frame.get("node_outputs") or {}),
            "foreach": {
                "loop_index": int(frame.get("loop_index") or 0),
                "items_fingerprint": str(frame.get("items_fingerprint") or ""),
                "completed_results": deepcopy(frame.get("completed_results") or []),
                "child_resume": checkpoint,
            },
        }
    return checkpoint


def _persist_graph_retry_checkpoint(
    db,
    *,
    run_id: int,
    root_step_index: int,
    root_state: dict,
    lineage: list,
    graph_agent_id: int | None,
    node_id: str,
    node_type: str,
    node_scope: str,
    node_outputs: dict,
    params: dict,
    resolved: bool,
) -> None:
    run = db.get(AutomationRun, int(run_id))
    if run is None:
        # execute_step is also used directly by isolated previews/tests. Durable
        # retry is a full AutomationRun feature, so direct step execution stays
        # side-effect compatible without manufacturing a fake run.
        return

    safe, reason = _graph_retry_safety(
        db,
        graph_agent_id=graph_agent_id,
        node_type=node_type,
        params=params,
        resolved=resolved,
    )
    output = deepcopy(run.output_data if isinstance(run.output_data, dict) else {})
    workflow_fingerprint = str(output.get("workflow_fingerprint") or "").strip()
    if not workflow_fingerprint:
        for checkpoint_key in ("approval", "wait", "event_wait"):
            prior = output.get(checkpoint_key)
            if isinstance(prior, dict):
                workflow_fingerprint = str(
                    prior.get("workflow_fingerprint") or ""
                ).strip()
                if workflow_fingerprint:
                    break
    output["retry_checkpoint"] = {
        "version": RETRY_CHECKPOINT_VERSION,
        "workflow_fingerprint": workflow_fingerprint,
        "workflow_step_index": int(root_step_index),
        "graph_resume": _compose_retry_graph_resume(
            lineage=lineage,
            node_id=node_id,
            node_outputs=node_outputs,
        ),
        "state": deepcopy(dict(root_state or {})),
        "failed_node_id": str(node_id or ""),
        "failed_node_type": str(node_type or ""),
        "failed_node_scope": str(node_scope or ""),
        "safe": bool(safe),
        "reason": reason,
    }
    run.output_data = output
    db.flush()
    # The checkpoint is intentionally durable before the next node starts.
    # This also commits successful database effects from prior nodes so retry can
    # skip them instead of replaying work whose outcome is already known.
    db.commit()


def _store_failed_run(
    db,
    *,
    run_id: int,
    state: dict,
    step_results: list,
    trace: dict,
    error: Exception,
    extra_output: dict | None = None,
) -> AutomationRun:
    error_message = str(error)[:2000]
    db.rollback()

    run = db.get(AutomationRun, int(run_id))
    if run is None:
        raise RuntimeError("Durable automation run disappeared after failure") from error

    persisted = deepcopy(run.output_data if isinstance(run.output_data, dict) else {})
    checkpoint = (
        persisted.get("retry_checkpoint")
        if isinstance(persisted.get("retry_checkpoint"), dict)
        else None
    )
    spans = trace.get("spans") if isinstance(trace.get("spans"), list) else []
    if checkpoint and spans:
        last = spans[-1] if isinstance(spans[-1], dict) else None
        if (
            isinstance(last, dict)
            and str(last.get("status") or "").lower() == "failed"
            and not last.get("node_id")
        ):
            last["node_id"] = checkpoint.get("failed_node_id") or None

    run.status = "failed"
    run.resume_at = None
    run.resume_event_name = None
    run.error_message = error_message
    run.finished_at = _utcnow_naive()
    trace["status"] = "failed"
    trace["finished_at"] = _trace_iso(run.finished_at)

    output = {
        **persisted,
        "state": deepcopy(dict(state or {})),
        "steps": deepcopy(list(step_results or [])),
        "trace": deepcopy(trace),
        "usage_recorded": True,
    }
    if isinstance(extra_output, dict):
        output.update(deepcopy(extra_output))
    run.output_data = output
    db.commit()
    db.refresh(run)
    return run


WAIT_UNIT_SECONDS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 60 * 60,
    "days": 24 * 60 * 60,
    "weeks": 7 * 24 * 60 * 60,
}
MAX_WAIT_SECONDS = 365 * 24 * 60 * 60


def _wait_resume_at(params: dict, *, now: datetime | None = None) -> datetime:
    current = now or _utcnow_naive()
    raw_until = str(params.get("until") or "").strip()
    has_duration = params.get("duration") is not None
    if bool(raw_until) == bool(has_duration):
        raise ValueError("Wait requires exactly one of until or duration")

    if raw_until:
        try:
            parsed = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Wait until must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("Wait until must include a timezone offset")
        return parsed.astimezone(UTC).replace(tzinfo=None)

    try:
        amount = float(params.get("duration"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Wait duration must be numeric") from exc
    unit = str(params.get("unit") or "seconds").strip().lower()
    multiplier = WAIT_UNIT_SECONDS.get(unit)
    if multiplier is None:
        raise ValueError("Unsupported wait duration unit")
    seconds = amount * multiplier
    if seconds <= 0 or seconds > MAX_WAIT_SECONDS:
        raise ValueError("Wait duration must be positive and at most one year")
    return current + timedelta(seconds=seconds)


class AutomationWaitRequired(RuntimeError):
    def __init__(
        self,
        *,
        resume_at: datetime,
        workflow_step_index: int,
        node_id: str,
        node_outputs: dict,
        graph_resume: dict | None = None,
    ):
        super().__init__(f"Wait required until {_trace_iso(resume_at)}")
        self.resume_at = resume_at
        self.workflow_step_index = int(workflow_step_index)
        self.node_id = str(node_id)
        self.node_outputs = deepcopy(dict(node_outputs or {}))
        self.graph_resume = deepcopy(
            graph_resume
            if isinstance(graph_resume, dict)
            else {
                "node_id": self.node_id,
                "node_outputs": self.node_outputs,
                "wait_completed": True,
                "resume_at": _trace_iso(resume_at),
            }
        )


def _store_wait_checkpoint(
    db,
    *,
    run: AutomationRun,
    workflow: AutomationWorkflow,
    wait: AutomationWaitRequired,
    state: dict,
    step_results: list,
    trace: dict,
) -> AutomationRun:
    run.status = "waiting_time"
    run.resume_at = wait.resume_at
    run.resume_event_name = None
    run.error_message = None
    run.finished_at = None
    trace["status"] = "waiting_time"
    trace["finished_at"] = None
    run.output_data = {
        "state": state,
        "steps": step_results,
        "trace": trace,
        "wait": {
            "resume_at": _trace_iso(wait.resume_at),
            "workflow_step_index": wait.workflow_step_index,
            "node_id": wait.node_id,
            "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
            "node_outputs": wait.node_outputs,
            "graph_resume": wait.graph_resume,
            "status": "waiting_time",
        },
    }
    db.commit()
    db.refresh(run)
    return run


def _store_approval_checkpoint(
    db,
    *,
    run: AutomationRun,
    workflow: AutomationWorkflow,
    approval: "AutomationApprovalRequired",
    state: dict,
    step_results: list,
    trace: dict,
) -> AutomationRun:
    request = ActionRequest(
        company_id=run.company_id,
        agent_id=approval.agent_id,
        conversation_id=None,
        action_type=approval.action_type,
        details={
            **approval.arguments,
            "_xvond_automation": {
                "run_id": run.id,
                "workflow_id": workflow.id,
                "workflow_step_index": approval.workflow_step_index,
                "node_id": approval.node_id,
                "approval_scope": approval.approval_scope,
                "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                "execution_key": state.get("_xvond_execution_key"),
            },
        },
        summary=approval.summary,
        status="awaiting_confirmation",
    )
    db.add(request)
    db.flush()
    run.status = "waiting_approval"
    run.resume_at = None
    run.resume_event_name = None
    run.error_message = None
    run.finished_at = None
    trace["status"] = "waiting_approval"
    trace["finished_at"] = None
    run.output_data = {
        "state": state,
        "steps": step_results,
        "trace": trace,
        "approval": {
            "request_id": request.id,
            "agent_id": approval.agent_id,
            "action_type": approval.action_type,
            "summary": approval.summary,
            "workflow_step_index": approval.workflow_step_index,
            "node_id": approval.node_id,
            "approval_scope": approval.approval_scope,
            "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
            "node_outputs": approval.node_outputs,
            "graph_resume": approval.graph_resume,
            "status": "awaiting_confirmation",
        },
    }
    db.commit()
    db.refresh(run)
    return run


def event_payload_matches(payload: dict | None, match: dict | None) -> bool:
    source = payload if isinstance(payload, dict) else {}
    rules = match if isinstance(match, dict) else {}
    for path, expected in rules.items():
        if extract_data_path(source, str(path)) != expected:
            return False
    return True


def _mark_event_received(
    graph_resume: dict,
    *,
    event_name: str,
    event_id: str,
    payload: dict,
) -> dict:
    checkpoint = deepcopy(graph_resume)
    foreach = checkpoint.get("foreach")
    if isinstance(foreach, dict) and isinstance(foreach.get("child_resume"), dict):
        foreach["child_resume"] = _mark_event_received(
            foreach["child_resume"],
            event_name=event_name,
            event_id=event_id,
            payload=payload,
        )
        checkpoint["foreach"] = foreach
        return checkpoint

    checkpoint["event_received"] = True
    checkpoint["event_name"] = str(event_name)
    checkpoint["event_id"] = str(event_id)
    checkpoint["event_payload"] = deepcopy(payload)
    return checkpoint


class AutomationEventRequired(RuntimeError):
    def __init__(
        self,
        *,
        event_name: str,
        match: dict,
        workflow_step_index: int,
        node_id: str,
        node_outputs: dict,
        graph_resume: dict | None = None,
    ):
        clean_event = str(event_name or "").strip().lower()
        if not clean_event or len(clean_event) > 120:
            raise ValueError("Awaited event name is invalid")
        clean_match: dict[str, str | int | float | bool | None] = {}
        for raw_path, expected in (match or {}).items():
            path = str(raw_path or "").strip()
            if not path or len(path) > 200:
                raise ValueError("Awaited event match field is invalid")
            if not isinstance(expected, (str, int, float, bool)) and expected is not None:
                raise ValueError("Awaited event match values must be scalar")
            clean_match[path] = expected
            if len(clean_match) >= 20:
                break

        super().__init__(f"Event required: {clean_event}")
        self.event_name = clean_event
        self.match = clean_match
        self.workflow_step_index = int(workflow_step_index)
        self.node_id = str(node_id)
        self.node_outputs = deepcopy(dict(node_outputs or {}))
        self.graph_resume = deepcopy(
            graph_resume
            if isinstance(graph_resume, dict)
            else {
                "node_id": self.node_id,
                "node_outputs": self.node_outputs,
                "event_received": False,
                "event_name": self.event_name,
            }
        )


def _store_event_checkpoint(
    db,
    *,
    run: AutomationRun,
    workflow: AutomationWorkflow,
    event_wait: AutomationEventRequired,
    state: dict,
    step_results: list,
    trace: dict,
) -> AutomationRun:
    run.status = "waiting_event"
    run.resume_at = None
    run.resume_event_name = event_wait.event_name
    run.error_message = None
    run.finished_at = None
    trace["status"] = "waiting_event"
    trace["finished_at"] = None
    run.output_data = {
        "state": state,
        "steps": step_results,
        "trace": trace,
        "event_wait": {
            "event_name": event_wait.event_name,
            "match": event_wait.match,
            "workflow_step_index": event_wait.workflow_step_index,
            "node_id": event_wait.node_id,
            "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
            "node_outputs": event_wait.node_outputs,
            "graph_resume": event_wait.graph_resume,
            "status": "waiting_event",
        },
    }
    db.commit()
    db.refresh(run)
    return run


class AutomationApprovalRequired(RuntimeError):
    def __init__(
        self,
        *,
        agent_id: int,
        action_type: str,
        arguments: dict,
        summary: str,
        workflow_step_index: int,
        node_id: str,
        node_outputs: dict,
        approval_scope: str | None = None,
        graph_resume: dict | None = None,
    ):
        super().__init__(f"Approval required for {action_type}")
        self.agent_id = int(agent_id)
        self.action_type = str(action_type)
        self.arguments = deepcopy(dict(arguments or {}))
        self.summary = str(summary or action_type)[:2000]
        self.workflow_step_index = int(workflow_step_index)
        self.node_id = str(node_id)
        self.node_outputs = deepcopy(dict(node_outputs or {}))
        self.approval_scope = str(approval_scope or node_id or "")
        self.graph_resume = deepcopy(
            graph_resume
            if isinstance(graph_resume, dict)
            else {
                "node_id": self.node_id,
                "node_outputs": self.node_outputs,
            }
        )


class AutomationRuntime:
    def execute(
        self,
        db,
        company_id: int,
        workflow: AutomationWorkflow,
        input_data: dict | None = None,
    ):
        if workflow.company_id != company_id:
            raise ValueError("Workflow does not belong to company")
        if not workflow.enabled:
            raise ValueError("Workflow is disabled")

        trigger_config = (
            workflow.trigger_config
            if isinstance(workflow.trigger_config, dict)
            else {}
        )
        routine_defaults = (
            dict(trigger_config.get("_xvond_runtime_inputs") or {})
            if isinstance(trigger_config.get("_xvond_runtime_inputs"), dict)
            else {}
        )
        # Compiled routine inputs are defaults, not a second hidden state.
        # Trigger/manual payload values may intentionally override them.
        original_input = {
            **routine_defaults,
            **dict(input_data or {}),
        }
        billing_service, billing_metric = _billing_contract(workflow)
        service_limits.record(
            db,
            company_id,
            billing_service,
            billing_metric,
            quantity=1,
            metadata={"workflow_id": workflow.id},
        )

        run = AutomationRun(
            company_id=company_id,
            workflow_id=workflow.id,
            status="running",
            input_data=original_input,
            output_data={},
        )
        db.add(run)
        db.flush()
        run_id = run.id

        state = dict(original_input)
        if not str(state.get("_xvond_execution_key") or "").strip():
            state["_xvond_execution_key"] = (
                f"automation:{company_id}:{workflow.id}:run:{run_id}"
            )
            original_input = dict(state)
            run.input_data = dict(original_input)
        step_results = []
        execution_key = str(state.get("_xvond_execution_key") or "")
        trace = _new_trace(
            company_id=company_id,
            workflow=workflow,
            execution_key=execution_key,
        )
        workflow_fingerprint = _checkpoint_fingerprint(workflow.steps or [])
        run.output_data = {
            "state": deepcopy(state),
            "steps": [],
            "trace": deepcopy(trace),
            "workflow_fingerprint": workflow_fingerprint,
        }
        # Persist the run and its billed usage before any business node starts.
        # Later node checkpoints can then survive a rollback of the failed node.
        db.commit()
        db.refresh(run)

        try:
            for index, step in enumerate(workflow.steps or []):
                span_started_at = _trace_iso()
                span_started_perf = perf_counter()
                try:
                    result = self.execute_step(
                        db,
                        company_id,
                        step,
                        state,
                        run_id=run_id,
                        step_index=index,
                    )
                except AutomationEventRequired as event_wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_event",
                        node_id=event_wait.node_id,
                    )
                    raise
                except AutomationWaitRequired as wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_time",
                        node_id=wait.node_id,
                    )
                    raise
                except AutomationApprovalRequired as approval:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_approval",
                        node_id=approval.node_id,
                    )
                    raise
                except Exception as exc:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="failed",
                        error=str(exc),
                    )
                    raise
                _append_step_span(
                    trace,
                    index=index,
                    step=step,
                    started_at=span_started_at,
                    started_perf=span_started_perf,
                    status="success",
                )
                step_results.append(
                    {
                        "index": index,
                        "type": step.get("type"),
                        "label": step.get("label"),
                        "result": result,
                    }
                )
                if isinstance(result, dict):
                    state.update(result)
                run.output_data = {
                    "state": deepcopy(state),
                    "steps": deepcopy(step_results),
                    "trace": deepcopy(trace),
                    "workflow_fingerprint": workflow_fingerprint,
                }
                db.commit()

            run.status = "success"
            run.resume_at = None
            run.resume_event_name = None
            run.finished_at = _utcnow_naive()
            trace["finished_at"] = _trace_iso(run.finished_at)
            trace["status"] = "success"
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "workflow_fingerprint": workflow_fingerprint,
            }
            db.commit()
            db.refresh(run)
            return run
        except AutomationApprovalRequired as approval:
            request = ActionRequest(
                company_id=company_id,
                agent_id=approval.agent_id,
                conversation_id=None,
                action_type=approval.action_type,
                details={
                    **approval.arguments,
                    "_xvond_automation": {
                        "run_id": run.id,
                        "workflow_id": workflow.id,
                        "workflow_step_index": approval.workflow_step_index,
                        "node_id": approval.node_id,
                        "approval_scope": approval.approval_scope,
                        "workflow_fingerprint": _checkpoint_fingerprint(
                            workflow.steps or []
                        ),
                        "execution_key": state.get("_xvond_execution_key"),
                    },
                },
                summary=approval.summary,
                status="awaiting_confirmation",
            )
            db.add(request)
            db.flush()
            run.status = "waiting_approval"
            run.resume_at = None
            run.resume_event_name = None
            trace["status"] = "waiting_approval"
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "approval": {
                    "request_id": request.id,
                    "agent_id": approval.agent_id,
                    "action_type": approval.action_type,
                    "summary": approval.summary,
                    "workflow_step_index": approval.workflow_step_index,
                    "node_id": approval.node_id,
                    "approval_scope": approval.approval_scope,
                    "workflow_fingerprint": _checkpoint_fingerprint(
                        workflow.steps or []
                    ),
                    "node_outputs": approval.node_outputs,
                    "graph_resume": approval.graph_resume,
                    "status": "awaiting_confirmation",
                },
            }
            run.error_message = None
            run.finished_at = None
            db.commit()
            db.refresh(run)
            return run
        except AutomationEventRequired as event_wait:
            return _store_event_checkpoint(
                db,
                run=run,
                workflow=workflow,
                event_wait=event_wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationWaitRequired as wait:
            return _store_wait_checkpoint(
                db,
                run=run,
                workflow=workflow,
                wait=wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except Exception as original_error:
            _store_failed_run(
                db,
                run_id=run_id,
                state=state,
                step_results=step_results,
                trace=trace,
                error=original_error,
            )
            raise

    def retry_failed(
        self,
        db,
        *,
        company_id: int,
        workflow: AutomationWorkflow,
        run: AutomationRun,
    ) -> AutomationRun:
        if run.company_id != company_id or workflow.company_id != company_id:
            raise ValueError("Failed run does not belong to company")
        if run.workflow_id != workflow.id:
            raise ValueError("Failed run does not belong to workflow")
        if run.status != "failed":
            raise ValueError("Automation run is not failed")
        if not workflow.enabled:
            raise ValueError("Automation workflow is disabled")

        output = deepcopy(run.output_data if isinstance(run.output_data, dict) else {})
        checkpoint = output.get("retry_checkpoint")
        if not isinstance(checkpoint, dict):
            raise ValueError("This failed run has no durable retry checkpoint")
        if int(checkpoint.get("version") or 0) != RETRY_CHECKPOINT_VERSION:
            raise ValueError("This failed run uses an unsupported retry checkpoint")
        if not bool(checkpoint.get("safe")):
            raise ValueError(
                str(checkpoint.get("reason") or "").strip()
                or "Xvond cannot prove that retrying this node is safe"
            )

        current_fingerprint = _checkpoint_fingerprint(workflow.steps or [])
        saved_fingerprint = str(
            checkpoint.get("workflow_fingerprint")
            or output.get("workflow_fingerprint")
            or ""
        ).strip()
        if not saved_fingerprint:
            raise ValueError("Retry checkpoint does not identify the workflow build")
        if saved_fingerprint != current_fingerprint:
            raise ValueError("Automation workflow changed after the failed checkpoint")

        try:
            step_index = int(checkpoint.get("workflow_step_index"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Retry checkpoint has an invalid workflow step") from exc
        if not 0 <= step_index < len(workflow.steps or []):
            raise ValueError("Retry checkpoint workflow step is out of range")
        step = (workflow.steps or [])[step_index]
        if str(step.get("type") or "").strip().lower() != "graph":
            raise ValueError("Retry checkpoint does not point to a graph workflow step")

        graph_resume = checkpoint.get("graph_resume")
        if not isinstance(graph_resume, dict) or not str(
            graph_resume.get("node_id") or ""
        ).strip():
            raise ValueError("Retry checkpoint is missing its graph resume state")

        state = dict(run.input_data or {})
        checkpoint_state = checkpoint.get("state")
        if isinstance(checkpoint_state, dict):
            state.update(deepcopy(checkpoint_state))
        for key in _RETRY_TRANSIENT_STATE_KEYS:
            state.pop(key, None)
        state["_xvond_graph_resume"] = {
            "workflow_step_index": step_index,
            **deepcopy(graph_resume),
        }

        step_results = list(output.get("steps") or [])
        execution_key = str(state.get("_xvond_execution_key") or "")
        if not execution_key:
            raise ValueError("Retry checkpoint lost the stable execution identity")

        trace = deepcopy(output.get("trace") or {})
        if not isinstance(trace, dict) or not trace.get("trace_id"):
            trace = _new_trace(
                company_id=company_id,
                workflow=workflow,
                execution_key=execution_key,
            )
        trace["status"] = "running"
        trace["finished_at"] = None

        attempts = int(output.get("retry_attempts") or 0) + 1
        run.status = "running"
        run.resume_at = None
        run.resume_event_name = None
        run.error_message = None
        run.finished_at = None
        run.output_data = {
            **output,
            "trace": deepcopy(trace),
            "retry_attempts": attempts,
            "retry": {
                "status": "running",
                "attempt": attempts,
                "from_node": checkpoint.get("failed_node_id"),
                "from_scope": checkpoint.get("failed_node_scope"),
            },
        }
        db.commit()
        db.refresh(run)

        try:
            for index in range(step_index, len(workflow.steps or [])):
                current_step = (workflow.steps or [])[index]
                span_started_at = _trace_iso()
                span_started_perf = perf_counter()
                try:
                    result = self.execute_step(
                        db,
                        company_id,
                        current_step,
                        state,
                        run_id=run.id,
                        step_index=index,
                    )
                except AutomationEventRequired as event_wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=current_step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_event",
                        phase="retry",
                        node_id=event_wait.node_id,
                    )
                    raise
                except AutomationWaitRequired as wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=current_step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_time",
                        phase="retry",
                        node_id=wait.node_id,
                    )
                    raise
                except AutomationApprovalRequired as approval:
                    _append_step_span(
                        trace,
                        index=index,
                        step=current_step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_approval",
                        phase="retry",
                        node_id=approval.node_id,
                    )
                    raise
                except Exception as exc:
                    _append_step_span(
                        trace,
                        index=index,
                        step=current_step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="failed",
                        phase="retry",
                        error=str(exc),
                    )
                    raise

                _append_step_span(
                    trace,
                    index=index,
                    step=current_step,
                    started_at=span_started_at,
                    started_perf=span_started_perf,
                    status="success",
                    phase="retry",
                )
                step_results.append(
                    {
                        "index": index,
                        "type": current_step.get("type"),
                        "label": current_step.get("label"),
                        "result": result,
                    }
                )
                if isinstance(result, dict):
                    state.update(result)
                state.pop("_xvond_graph_resume", None)

                run.output_data = {
                    "state": deepcopy(state),
                    "steps": deepcopy(step_results),
                    "trace": deepcopy(trace),
                    "workflow_fingerprint": current_fingerprint,
                    "retry_attempts": attempts,
                    "retry": {
                        "status": "running",
                        "attempt": attempts,
                    },
                }
                db.commit()

            run.status = "success"
            run.resume_at = None
            run.resume_event_name = None
            run.finished_at = _utcnow_naive()
            trace["status"] = "success"
            trace["finished_at"] = _trace_iso(run.finished_at)
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "workflow_fingerprint": current_fingerprint,
                "retry_attempts": attempts,
                "retry": {
                    "status": "succeeded",
                    "attempt": attempts,
                },
            }
            db.commit()
            db.refresh(run)
            return run
        except AutomationEventRequired as event_wait:
            state.pop("_xvond_graph_resume", None)
            return _store_event_checkpoint(
                db,
                run=run,
                workflow=workflow,
                event_wait=event_wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationWaitRequired as wait:
            state.pop("_xvond_graph_resume", None)
            return _store_wait_checkpoint(
                db,
                run=run,
                workflow=workflow,
                wait=wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationApprovalRequired as approval:
            state.pop("_xvond_graph_resume", None)
            return _store_approval_checkpoint(
                db,
                run=run,
                workflow=workflow,
                approval=approval,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except Exception as exc:
            _store_failed_run(
                db,
                run_id=run.id,
                state=state,
                step_results=step_results,
                trace=trace,
                error=exc,
                extra_output={
                    "retry_attempts": attempts,
                    "retry": {
                        "status": "failed",
                        "attempt": attempts,
                    },
                },
            )
            raise

    def resume_approval(
        self,
        db,
        *,
        company_id: int,
        workflow: AutomationWorkflow,
        run: AutomationRun,
        request: ActionRequest,
    ) -> AutomationRun:
        if run.company_id != company_id or workflow.company_id != company_id:
            raise ValueError("Approval run does not belong to company")
        if run.workflow_id != workflow.id:
            raise ValueError("Approval run does not belong to workflow")
        if run.status != "waiting_approval":
            raise ValueError("Automation run is not waiting for approval")
        if request.company_id != company_id:
            raise ValueError("Approval request does not belong to company")
        if request.status != "approved":
            raise ValueError("Approval request has not been approved")

        output = dict(run.output_data or {})
        approval = output.get("approval")
        if not isinstance(approval, dict):
            raise ValueError("Automation approval checkpoint is missing")
        if int(approval.get("request_id") or 0) != int(request.id):
            raise ValueError("Approval request does not match run checkpoint")

        saved_workflow_fingerprint = str(
            approval.get("workflow_fingerprint") or ""
        ).strip()
        if (
            saved_workflow_fingerprint
            and saved_workflow_fingerprint
            != _checkpoint_fingerprint(workflow.steps or [])
        ):
            raise ValueError(
                "Automation workflow changed after the approval checkpoint"
            )

        step_index = int(approval.get("workflow_step_index") or 0)
        if not 0 <= step_index < len(workflow.steps or []):
            raise ValueError("Automation approval step is invalid")

        state = dict(run.input_data or {})
        saved_state = output.get("state")
        if isinstance(saved_state, dict):
            state.update(saved_state)
        state["_xvond_approved_request_id"] = int(request.id)
        saved_graph_resume = approval.get("graph_resume")
        if isinstance(saved_graph_resume, dict):
            state["_xvond_graph_resume"] = {
                "workflow_step_index": step_index,
                **deepcopy(saved_graph_resume),
            }
        else:
            state["_xvond_graph_resume"] = {
                "workflow_step_index": step_index,
                "node_id": str(approval.get("node_id") or ""),
                "node_outputs": deepcopy(approval.get("node_outputs") or {}),
            }
        step_results = list(output.get("steps") or [])
        execution_key = str(state.get("_xvond_execution_key") or "")
        trace = deepcopy(output.get("trace") or {})
        if not isinstance(trace, dict) or not trace.get("trace_id"):
            trace = _new_trace(
                company_id=company_id,
                workflow=workflow,
                execution_key=execution_key,
            )
        trace["status"] = "running"
        trace["finished_at"] = None

        run.status = "running"
        run.resume_at = None
        run.resume_event_name = None
        run.error_message = None
        db.flush()

        try:
            for index in range(step_index, len(workflow.steps or [])):
                step = (workflow.steps or [])[index]
                span_started_at = _trace_iso()
                span_started_perf = perf_counter()
                try:
                    result = self.execute_step(
                        db,
                        company_id,
                        step,
                        state,
                        run_id=run.id,
                        step_index=index,
                    )
                except AutomationEventRequired as event_wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_event",
                        phase="resume",
                        node_id=event_wait.node_id,
                    )
                    raise
                except AutomationWaitRequired as wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_time",
                        phase="resume",
                        node_id=wait.node_id,
                    )
                    raise
                except AutomationApprovalRequired as next_approval:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_approval",
                        phase="resume",
                        node_id=next_approval.node_id,
                    )
                    raise
                except Exception as exc:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="failed",
                        phase="resume",
                        error=str(exc),
                    )
                    raise
                _append_step_span(
                    trace,
                    index=index,
                    step=step,
                    started_at=span_started_at,
                    started_perf=span_started_perf,
                    status="success",
                    phase="resume",
                )
                step_results.append(
                    {
                        "index": index,
                        "type": step.get("type"),
                        "label": step.get("label"),
                        "result": result,
                    }
                )
                if isinstance(result, dict):
                    state.update(result)
                state.pop("_xvond_graph_resume", None)
                state.pop("_xvond_approved_request_id", None)
                run.output_data = {
                    "state": deepcopy(state),
                    "steps": deepcopy(step_results),
                    "trace": deepcopy(trace),
                    "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                }
                db.commit()

            run.status = "success"
            run.resume_at = None
            run.resume_event_name = None
            run.finished_at = _utcnow_naive()
            trace["status"] = "success"
            trace["finished_at"] = _trace_iso(run.finished_at)
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                "approval": {
                    **approval,
                    "status": "approved",
                },
            }
            db.commit()
            db.refresh(run)
            return run
        except AutomationEventRequired as event_wait:
            state.pop("_xvond_approved_request_id", None)
            state.pop("_xvond_graph_resume", None)
            return _store_event_checkpoint(
                db,
                run=run,
                workflow=workflow,
                event_wait=event_wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationWaitRequired as wait:
            state.pop("_xvond_approved_request_id", None)
            state.pop("_xvond_graph_resume", None)
            return _store_wait_checkpoint(
                db,
                run=run,
                workflow=workflow,
                wait=wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationApprovalRequired as next_approval:
            next_request = ActionRequest(
                company_id=company_id,
                agent_id=next_approval.agent_id,
                conversation_id=None,
                action_type=next_approval.action_type,
                details={
                    **next_approval.arguments,
                    "_xvond_automation": {
                        "run_id": run.id,
                        "workflow_id": workflow.id,
                        "workflow_step_index": next_approval.workflow_step_index,
                        "node_id": next_approval.node_id,
                        "approval_scope": next_approval.approval_scope,
                        "workflow_fingerprint": _checkpoint_fingerprint(
                            workflow.steps or []
                        ),
                        "execution_key": state.get("_xvond_execution_key"),
                    },
                },
                summary=next_approval.summary,
                status="awaiting_confirmation",
            )
            db.add(next_request)
            db.flush()
            state.pop("_xvond_approved_request_id", None)
            state.pop("_xvond_graph_resume", None)
            run.status = "waiting_approval"
            run.error_message = None
            trace["status"] = "waiting_approval"
            trace["finished_at"] = None
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "approval": {
                    "request_id": next_request.id,
                    "agent_id": next_approval.agent_id,
                    "action_type": next_approval.action_type,
                    "summary": next_approval.summary,
                    "workflow_step_index": next_approval.workflow_step_index,
                    "node_id": next_approval.node_id,
                    "approval_scope": next_approval.approval_scope,
                    "workflow_fingerprint": _checkpoint_fingerprint(
                        workflow.steps or []
                    ),
                    "node_outputs": next_approval.node_outputs,
                    "graph_resume": next_approval.graph_resume,
                    "status": "awaiting_confirmation",
                },
            }
            run.finished_at = None
            db.commit()
            db.refresh(run)
            return run
        except Exception as exc:
            _store_failed_run(
                db,
                run_id=run.id,
                state=state,
                step_results=step_results,
                trace=trace,
                error=exc,
                extra_output={
                    "approval": {
                        **approval,
                        "status": "approved_execution_failed",
                    },
                },
            )
            raise

    def resume_wait(
        self,
        db,
        *,
        company_id: int,
        workflow: AutomationWorkflow,
        run: AutomationRun,
        now: datetime | None = None,
    ) -> AutomationRun:
        if run.company_id != company_id or workflow.company_id != company_id:
            raise ValueError("Waiting run does not belong to company")
        if run.workflow_id != workflow.id:
            raise ValueError("Waiting run does not belong to workflow")
        if run.status != "waiting_time":
            raise ValueError("Automation run is not waiting for time")
        if run.resume_at is None:
            raise ValueError("Automation wait deadline is missing")

        current = now or _utcnow_naive()
        if current.tzinfo is not None:
            current = current.astimezone(UTC).replace(tzinfo=None)
        if run.resume_at > current:
            raise ValueError("Automation wait is not due yet")

        output = dict(run.output_data or {})
        wait_checkpoint = output.get("wait")
        if not isinstance(wait_checkpoint, dict):
            raise ValueError("Automation wait checkpoint is missing")

        saved_workflow_fingerprint = str(
            wait_checkpoint.get("workflow_fingerprint") or ""
        ).strip()
        if (
            saved_workflow_fingerprint
            and saved_workflow_fingerprint
            != _checkpoint_fingerprint(workflow.steps or [])
        ):
            raise ValueError("Automation workflow changed after the wait checkpoint")

        step_index = int(wait_checkpoint.get("workflow_step_index") or 0)
        if not 0 <= step_index < len(workflow.steps or []):
            raise ValueError("Automation wait step is invalid")

        state = dict(run.input_data or {})
        saved_state = output.get("state")
        if isinstance(saved_state, dict):
            state.update(saved_state)
        state.pop("_xvond_approved_request_id", None)
        state.pop("_xvond_graph_resume", None)

        saved_graph_resume = wait_checkpoint.get("graph_resume")
        if isinstance(saved_graph_resume, dict):
            state["_xvond_graph_resume"] = {
                "workflow_step_index": step_index,
                **deepcopy(saved_graph_resume),
            }
        else:
            state["_xvond_graph_resume"] = {
                "workflow_step_index": step_index,
                "node_id": str(wait_checkpoint.get("node_id") or ""),
                "node_outputs": deepcopy(wait_checkpoint.get("node_outputs") or {}),
                "wait_completed": True,
                "resume_at": str(wait_checkpoint.get("resume_at") or ""),
            }

        step_results = list(output.get("steps") or [])
        execution_key = str(state.get("_xvond_execution_key") or "")
        trace = deepcopy(output.get("trace") or {})
        if not isinstance(trace, dict) or not trace.get("trace_id"):
            trace = _new_trace(
                company_id=company_id,
                workflow=workflow,
                execution_key=execution_key,
            )
        trace["status"] = "running"
        trace["finished_at"] = None

        run.status = "running"
        run.resume_at = None
        run.resume_event_name = None
        run.error_message = None
        db.flush()

        try:
            for index in range(step_index, len(workflow.steps or [])):
                step = (workflow.steps or [])[index]
                span_started_at = _trace_iso()
                span_started_perf = perf_counter()
                try:
                    result = self.execute_step(
                        db,
                        company_id,
                        step,
                        state,
                        run_id=run.id,
                        step_index=index,
                    )
                except AutomationEventRequired as event_wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_event",
                        phase="resume_wait",
                        node_id=event_wait.node_id,
                    )
                    raise
                except AutomationWaitRequired as next_wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_time",
                        phase="resume_wait",
                        node_id=next_wait.node_id,
                    )
                    raise
                except AutomationApprovalRequired as approval:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_approval",
                        phase="resume_wait",
                        node_id=approval.node_id,
                    )
                    raise
                except Exception as exc:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="failed",
                        phase="resume_wait",
                        error=str(exc),
                    )
                    raise

                _append_step_span(
                    trace,
                    index=index,
                    step=step,
                    started_at=span_started_at,
                    started_perf=span_started_perf,
                    status="success",
                    phase="resume_wait",
                )
                step_results.append(
                    {
                        "index": index,
                        "type": step.get("type"),
                        "label": step.get("label"),
                        "result": result,
                    }
                )
                if isinstance(result, dict):
                    state.update(result)
                state.pop("_xvond_graph_resume", None)
                run.output_data = {
                    "state": deepcopy(state),
                    "steps": deepcopy(step_results),
                    "trace": deepcopy(trace),
                    "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                }
                db.commit()

            run.status = "success"
            run.resume_at = None
            run.resume_event_name = None
            run.finished_at = _utcnow_naive()
            trace["status"] = "success"
            trace["finished_at"] = _trace_iso(run.finished_at)
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                "wait": {
                    **wait_checkpoint,
                    "status": "resumed",
                },
            }
            db.commit()
            db.refresh(run)
            return run
        except AutomationEventRequired as event_wait:
            state.pop("_xvond_graph_resume", None)
            return _store_event_checkpoint(
                db,
                run=run,
                workflow=workflow,
                event_wait=event_wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationWaitRequired as next_wait:
            state.pop("_xvond_graph_resume", None)
            return _store_wait_checkpoint(
                db,
                run=run,
                workflow=workflow,
                wait=next_wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationApprovalRequired as approval:
            state.pop("_xvond_graph_resume", None)
            return _store_approval_checkpoint(
                db,
                run=run,
                workflow=workflow,
                approval=approval,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except Exception as exc:
            _store_failed_run(
                db,
                run_id=run.id,
                state=state,
                step_results=step_results,
                trace=trace,
                error=exc,
                extra_output={
                    "wait": {
                        **wait_checkpoint,
                        "status": "resume_failed",
                    },
                },
            )
            raise

    def resume_event(
        self,
        db,
        *,
        company_id: int,
        workflow: AutomationWorkflow,
        run: AutomationRun,
        event_name: str,
        event_id: str,
        payload: dict | None = None,
    ) -> AutomationRun:
        clean_event = str(event_name or "").strip().lower()
        clean_event_id = str(event_id or "").strip()
        event_payload = dict(payload or {})

        if run.company_id != company_id or workflow.company_id != company_id:
            raise ValueError("Event-wait run does not belong to company")
        if run.workflow_id != workflow.id:
            raise ValueError("Event-wait run does not belong to workflow")
        if run.status != "waiting_event":
            raise ValueError("Automation run is not waiting for an event")
        if str(run.resume_event_name or "").strip().lower() != clean_event:
            raise ValueError("Automation run is waiting for a different event")
        if not clean_event_id or len(clean_event_id) > 200:
            raise ValueError("Automation event id is invalid")

        output = dict(run.output_data or {})
        event_wait = output.get("event_wait")
        if not isinstance(event_wait, dict):
            raise ValueError("Automation event checkpoint is missing")
        match = event_wait.get("match")
        if not event_payload_matches(event_payload, match if isinstance(match, dict) else {}):
            raise ValueError("Automation event does not match the run checkpoint")

        saved_workflow_fingerprint = str(
            event_wait.get("workflow_fingerprint") or ""
        ).strip()
        if (
            saved_workflow_fingerprint
            and saved_workflow_fingerprint
            != _checkpoint_fingerprint(workflow.steps or [])
        ):
            raise ValueError("Automation workflow changed after the event checkpoint")

        step_index = int(event_wait.get("workflow_step_index") or 0)
        if not 0 <= step_index < len(workflow.steps or []):
            raise ValueError("Automation event wait step is invalid")

        state = dict(run.input_data or {})
        saved_state = output.get("state")
        if isinstance(saved_state, dict):
            state.update(saved_state)
        state.pop("_xvond_approved_request_id", None)
        state.pop("_xvond_graph_resume", None)

        saved_graph_resume = event_wait.get("graph_resume")
        if isinstance(saved_graph_resume, dict):
            state["_xvond_graph_resume"] = {
                "workflow_step_index": step_index,
                **_mark_event_received(
                    saved_graph_resume,
                    event_name=clean_event,
                    event_id=clean_event_id,
                    payload=event_payload,
                ),
            }
        else:
            state["_xvond_graph_resume"] = {
                "workflow_step_index": step_index,
                "node_id": str(event_wait.get("node_id") or ""),
                "node_outputs": deepcopy(event_wait.get("node_outputs") or {}),
                "event_received": True,
                "event_name": clean_event,
                "event_id": clean_event_id,
                "event_payload": deepcopy(event_payload),
            }

        step_results = list(output.get("steps") or [])
        execution_key = str(state.get("_xvond_execution_key") or "")
        trace = deepcopy(output.get("trace") or {})
        if not isinstance(trace, dict) or not trace.get("trace_id"):
            trace = _new_trace(
                company_id=company_id,
                workflow=workflow,
                execution_key=execution_key,
            )
        trace["status"] = "running"
        trace["finished_at"] = None

        run.status = "running"
        run.resume_at = None
        run.resume_event_name = None
        run.error_message = None
        db.flush()

        try:
            for index in range(step_index, len(workflow.steps or [])):
                step = (workflow.steps or [])[index]
                span_started_at = _trace_iso()
                span_started_perf = perf_counter()
                try:
                    result = self.execute_step(
                        db,
                        company_id,
                        step,
                        state,
                        run_id=run.id,
                        step_index=index,
                    )
                except AutomationEventRequired as next_event:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_event",
                        phase="resume_event",
                        node_id=next_event.node_id,
                    )
                    raise
                except AutomationWaitRequired as wait:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_time",
                        phase="resume_event",
                        node_id=wait.node_id,
                    )
                    raise
                except AutomationApprovalRequired as approval:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="waiting_approval",
                        phase="resume_event",
                        node_id=approval.node_id,
                    )
                    raise
                except Exception as exc:
                    _append_step_span(
                        trace,
                        index=index,
                        step=step,
                        started_at=span_started_at,
                        started_perf=span_started_perf,
                        status="failed",
                        phase="resume_event",
                        error=str(exc),
                    )
                    raise

                _append_step_span(
                    trace,
                    index=index,
                    step=step,
                    started_at=span_started_at,
                    started_perf=span_started_perf,
                    status="success",
                    phase="resume_event",
                )
                step_results.append(
                    {
                        "index": index,
                        "type": step.get("type"),
                        "label": step.get("label"),
                        "result": result,
                    }
                )
                if isinstance(result, dict):
                    state.update(result)
                state.pop("_xvond_graph_resume", None)
                run.output_data = {
                    "state": deepcopy(state),
                    "steps": deepcopy(step_results),
                    "trace": deepcopy(trace),
                    "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                }
                db.commit()

            run.status = "success"
            run.resume_at = None
            run.resume_event_name = None
            run.finished_at = _utcnow_naive()
            trace["status"] = "success"
            trace["finished_at"] = _trace_iso(run.finished_at)
            run.output_data = {
                "state": state,
                "steps": step_results,
                "trace": trace,
                "workflow_fingerprint": _checkpoint_fingerprint(workflow.steps or []),
                "event_wait": {
                    **event_wait,
                    "event_id": clean_event_id,
                    "status": "received",
                },
            }
            db.commit()
            db.refresh(run)
            return run
        except AutomationEventRequired as next_event:
            state.pop("_xvond_graph_resume", None)
            return _store_event_checkpoint(
                db,
                run=run,
                workflow=workflow,
                event_wait=next_event,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationWaitRequired as wait:
            state.pop("_xvond_graph_resume", None)
            return _store_wait_checkpoint(
                db,
                run=run,
                workflow=workflow,
                wait=wait,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except AutomationApprovalRequired as approval:
            state.pop("_xvond_graph_resume", None)
            return _store_approval_checkpoint(
                db,
                run=run,
                workflow=workflow,
                approval=approval,
                state=state,
                step_results=step_results,
                trace=trace,
            )
        except Exception as exc:
            _store_failed_run(
                db,
                run_id=run.id,
                state=state,
                step_results=step_results,
                trace=trace,
                error=exc,
                extra_output={
                    "event_wait": {
                        **event_wait,
                        "event_id": clean_event_id,
                        "status": "resume_failed",
                    },
                },
            )
            raise

    def execute_step(
        self,
        db,
        company_id: int,
        step: dict,
        state: dict,
        *,
        run_id: int,
        step_index: int,
    ):
        step_type = str(step.get("type", "")).strip().lower()

        if step_type == "graph":
            graph = normalize_execution_graph(step.get("graph") or {})
            nodes = graph.get("nodes") or []
            if not nodes:
                raise ValueError("Execution graph has no runnable nodes")

            resume = state.get("_xvond_graph_resume")
            resume = resume if isinstance(resume, dict) else {}
            raw_resume_step = resume.get("workflow_step_index")
            resume_step_index = (
                int(raw_resume_step)
                if raw_resume_step is not None
                else -1
            )
            resume_for_step = resume_step_index == int(step_index)
            node_outputs: dict[str, dict] = (
                deepcopy(resume.get("node_outputs") or {})
                if resume_for_step
                else {}
            )
            resume_node_id = str(resume.get("node_id") or "").strip() if resume_for_step else ""
            waiting_for_resume_node = bool(resume_node_id)
            graph_agent_id = step.get("agent_id")
            graph_path = str(state.get("_xvond_graph_path") or "").strip()
            preview_mode = bool(state.get("_xvond_preview"))
            preview_outputs = (
                state.get("_xvond_preview_outputs")
                if isinstance(state.get("_xvond_preview_outputs"), dict)
                else {}
            )
            retry_root_step_index = int(
                state.get("_xvond_retry_root_step_index")
                if state.get("_xvond_retry_root_step_index") is not None
                else step_index
            )
            stored_root_state = state.get("_xvond_retry_root_state")
            retry_root_state = (
                deepcopy(stored_root_state)
                if isinstance(stored_root_state, dict)
                else _retry_root_state(state)
            )
            stored_lineage = state.get("_xvond_retry_lineage")
            retry_lineage = (
                deepcopy(stored_lineage)
                if isinstance(stored_lineage, list)
                else []
            )
            for node_index, node in enumerate(nodes):
                node_id = str(node.get("id") or "").strip()
                is_resume_node = bool(
                    resume_for_step
                    and resume_node_id
                    and node_id == resume_node_id
                )
                if waiting_for_resume_node and not is_resume_node:
                    continue
                if waiting_for_resume_node and is_resume_node:
                    waiting_for_resume_node = False
                node_resume = resume if is_resume_node else {}
                node_scope = (
                    f"{graph_path}/{node_id}"
                    if graph_path
                    else node_id
                )
                node_type = str(node.get("type") or "").strip().lower()
                raw_node_params = (
                    node.get("params")
                    if isinstance(node.get("params"), dict)
                    else {}
                )
                if not preview_mode:
                    _persist_graph_retry_checkpoint(
                        db,
                        run_id=run_id,
                        root_step_index=retry_root_step_index,
                        root_state=retry_root_state,
                        lineage=retry_lineage,
                        graph_agent_id=graph_agent_id,
                        node_id=node_id,
                        node_type=node_type,
                        node_scope=node_scope,
                        node_outputs=node_outputs,
                        params=raw_node_params,
                        resolved=False,
                    )
                dependencies = list(node.get("depends_on") or [])
                if any(dep not in node_outputs for dep in dependencies):
                    raise ValueError(
                        f"Execution graph dependency is unavailable for node {node_id}"
                    )
                gate = resolve_graph_value(
                    node.get("when"),
                    state=state,
                    node_outputs=node_outputs,
                ) if "when" in node else True
                if not bool(gate):
                    node_outputs[node_id] = {"skipped": True}
                    continue

                raw_params = node.get("params") or {}
                if node_type == "foreach" and isinstance(raw_params, dict):
                    params = dict(raw_params)
                    params["items"] = resolve_graph_value(
                        raw_params.get("items"),
                        state=state,
                        node_outputs=node_outputs,
                    )
                    params["graph"] = raw_params.get("graph") or {}
                else:
                    params = resolve_graph_value(
                        raw_params,
                        state=state,
                        node_outputs=node_outputs,
                    )
                if not isinstance(params, dict):
                    params = {}

                if not preview_mode:
                    _persist_graph_retry_checkpoint(
                        db,
                        run_id=run_id,
                        root_step_index=retry_root_step_index,
                        root_state=retry_root_state,
                        lineage=retry_lineage,
                        graph_agent_id=graph_agent_id,
                        node_id=node_id,
                        node_type=node_type,
                        node_scope=node_scope,
                        node_outputs=node_outputs,
                        params=params,
                        resolved=True,
                    )

                if preview_mode and node_scope in preview_outputs:
                    override = deepcopy(preview_outputs[node_scope])
                    node_outputs[node_id] = (
                        {"preview_override": True, **override}
                        if isinstance(override, dict)
                        else {"preview_override": True, "result": override}
                    )
                    continue

                nested_step: dict = {"type": node_type}
                if node_type == "ai":
                    if preview_mode:
                        preview_ai = state.get("_xvond_preview_ai_executor")
                        if callable(preview_ai):
                            preview_result = preview_ai(
                                prompt=str(params.get("prompt") or node.get("label") or ""),
                                context=deepcopy(params.get("context")),
                                node_scope=node_scope,
                            )
                            if not isinstance(preview_result, dict):
                                raise ValueError(
                                    f"Execution graph preview AI node {node_id} returned an invalid result"
                                )
                            node_outputs[node_id] = {
                                "preview": True,
                                "simulated": False,
                                **preview_result,
                            }
                        else:
                            node_outputs[node_id] = {
                                "preview": True,
                                "simulated": True,
                                "ai_response": f"[simulated AI output for {node_scope}]",
                                "prompt": params.get("prompt") or node.get("label"),
                                "context": deepcopy(params.get("context")),
                            }
                        continue
                    nested_step = {
                        "type": "ai",
                        "agent_id": params.get("agent_id") or graph_agent_id,
                        "prompt": params.get("prompt") or node.get("label"),
                        "context": params.get("context"),
                    }
                elif node_type == "media":
                    if preview_mode:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "media_url": f"preview://media/{node_scope}",
                            "generated_media": {
                                "model": params.get("model"),
                                "size": params.get("size") or "1024x1024",
                                "prompt": params.get("prompt") or node.get("label"),
                            },
                        }
                        continue
                    nested_step = {
                        "type": "media_generation",
                        "prompt": params.get("prompt") or node.get("label"),
                        "context": params.get("context"),
                        "model": params.get("model"),
                        "size": params.get("size") or "1024x1024",
                    }
                elif node_type == "action":
                    action_agent_id = int(params.get("agent_id") or graph_agent_id or 0)
                    action_type = str(params.get("action_type") or "").strip()
                    if not action_agent_id or not action_type:
                        raise ValueError(
                            f"Execution graph action node {node_id} requires agent_id and action_type"
                        )
                    if preview_mode:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "scheduled_action_result": {
                                "would_execute": True,
                                "agent_id": action_agent_id,
                                "action_type": action_type,
                                "arguments": deepcopy(params.get("arguments") or {}),
                                "approval_required_at_runtime": True,
                            },
                        }
                        continue
                    nested_step = {
                        "type": "scheduled_action",
                        "agent_id": action_agent_id,
                        "action_type": action_type,
                        "arguments": params.get("arguments") or {},
                        "approval_request_id": (
                            int(state.get("_xvond_approved_request_id") or 0) or None
                        ),
                        "approval_node_id": node_id,
                        "approval_scope": node_scope,
                        "_xvond_graph_action": True,
                    }
                elif node_type == "http_get_json":
                    url = str(params.get("url") or "").strip()
                    if not url:
                        raise ValueError(f"Execution graph node {node_id} requires url")
                    result = safe_http_request(
                        url=url,
                        method="GET",
                        headers={"Accept": "application/json"},
                        timeout=float(params.get("timeout") or 15),
                        max_response_bytes=500_000,
                    )
                    status = int(result.get("status_code") or 0)
                    if not 200 <= status < 300:
                        raise ValueError(
                            f"Execution graph HTTP node {node_id} returned HTTP {status}"
                        )
                    import json
                    try:
                        body = json.loads(result.get("response") or "{}")
                    except ValueError as exc:
                        raise ValueError(
                            f"Execution graph HTTP node {node_id} returned invalid JSON"
                        ) from exc
                    node_result = {"result": body, "status_code": status}
                    node_outputs[node_id] = node_result
                    continue
                elif node_type == "web_fetch":
                    url = str(params.get("url") or "").strip()
                    if not url:
                        raise ValueError(f"Execution graph node {node_id} requires url")
                    result = safe_http_request(
                        url=url,
                        method="GET",
                        headers={
                            "Accept": "text/html,text/plain,application/xhtml+xml;q=0.9,*/*;q=0.1",
                            "User-Agent": "Xvond-Agent/1.0",
                        },
                        timeout=float(params.get("timeout") or 15),
                        max_response_bytes=500_000,
                    )
                    status = int(result.get("status_code") or 0)
                    if not 200 <= status < 300:
                        raise ValueError(
                            f"Execution graph web fetch node {node_id} returned HTTP {status}"
                        )
                    node_outputs[node_id] = {
                        "content": str(result.get("response") or ""),
                        "status_code": status,
                        "truncated": bool(result.get("truncated")),
                    }
                    continue
                elif node_type == "browser":
                    start_url = str(params.get("url") or "").strip()
                    actions = params.get("actions") or []
                    if not isinstance(actions, list):
                        raise ValueError(
                            f"Execution graph browser node {node_id} actions must be a list"
                        )
                    mutating_ops = {"click", "fill", "press", "select"}
                    requires_approval = any(
                        isinstance(item, dict)
                        and str(item.get("op") or "").strip().lower() in mutating_ops
                        for item in actions
                    )
                    allow_interactions = False
                    if preview_mode and requires_approval:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "browser": {
                                "url": start_url,
                                "actions": deepcopy(actions),
                                "would_interact": True,
                                "approval_required_at_runtime": True,
                            },
                        }
                        continue
                    if requires_approval:
                        browser_agent_id = int(
                            params.get("agent_id") or graph_agent_id or 0
                        )
                        if not browser_agent_id:
                            raise ValueError(
                                f"Execution graph browser node {node_id} requires agent_id for interactive actions"
                            )
                        approval_request_id = int(
                            state.get("_xvond_approved_request_id") or 0
                        )
                        approval = None
                        if approval_request_id:
                            approval = (
                                db.query(ActionRequest)
                                .filter(
                                    ActionRequest.id == approval_request_id,
                                    ActionRequest.company_id == company_id,
                                    ActionRequest.agent_id == browser_agent_id,
                                    ActionRequest.action_type == "browser_interaction",
                                    ActionRequest.status == "approved",
                                )
                                .first()
                            )
                            if approval is not None and not _approval_checkpoint_matches(
                                approval,
                                node_id=node_id,
                                approval_scope=node_scope,
                            ):
                                approval = None
                        if approval is None:
                            raise AutomationApprovalRequired(
                                agent_id=browser_agent_id,
                                action_type="browser_interaction",
                                arguments={
                                    "url": start_url,
                                    "actions": actions,
                                },
                                summary=str(
                                    params.get("summary")
                                    or node.get("label")
                                    or "Approve browser interaction"
                                ),
                                workflow_step_index=step_index,
                                node_id=node_id,
                                node_outputs=node_outputs,
                                approval_scope=node_scope,
                            )
                        allow_interactions = True

                    try:
                        browser_result = run_browser_task(
                            start_url=start_url,
                            actions=actions,
                            timeout_seconds=int(params.get("timeout_seconds") or 30),
                            allow_interactions=allow_interactions,
                        )
                    except BrowserExecutionError as exc:
                        raise ValueError(
                            f"Execution graph browser node {node_id} failed: {exc}"
                        ) from exc
                    node_outputs[node_id] = browser_result
                    continue
                elif node_type == "state_read":
                    agent_id = int(params.get("agent_id") or graph_agent_id or 0)
                    if not agent_id:
                        raise ValueError(
                            f"Execution graph state_read node {node_id} requires agent_id"
                        )
                    namespace = str(params.get("namespace") or "default").strip()
                    key = str(params.get("key") or "").strip()
                    if not key:
                        raise ValueError(
                            f"Execution graph state_read node {node_id} requires key"
                        )
                    value = read_agent_state(
                        db,
                        company_id=company_id,
                        agent_id=agent_id,
                        namespace=namespace,
                        key=key,
                        default=params.get("default"),
                    )
                    node_outputs[node_id] = {
                        "value": value,
                        "namespace": namespace,
                        "key": key,
                    }
                    continue
                elif node_type == "state_write":
                    agent_id = int(params.get("agent_id") or graph_agent_id or 0)
                    if not agent_id:
                        raise ValueError(
                            f"Execution graph state_write node {node_id} requires agent_id"
                        )
                    namespace = str(params.get("namespace") or "default").strip()
                    key = str(params.get("key") or "").strip()
                    if not key:
                        raise ValueError(
                            f"Execution graph state_write node {node_id} requires key"
                        )
                    if preview_mode:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "value": deepcopy(params.get("value")),
                            "namespace": namespace,
                            "key": key,
                            "written": False,
                            "would_write": True,
                        }
                        continue
                    value = write_agent_state(
                        db,
                        company_id=company_id,
                        agent_id=agent_id,
                        namespace=namespace,
                        key=key,
                        value=params.get("value"),
                    )
                    node_outputs[node_id] = {
                        "value": value,
                        "namespace": namespace,
                        "key": key,
                        "written": True,
                    }
                    continue
                elif node_type == "state_delete":
                    agent_id = int(params.get("agent_id") or graph_agent_id or 0)
                    if not agent_id:
                        raise ValueError(
                            f"Execution graph state_delete node {node_id} requires agent_id"
                        )
                    namespace = str(params.get("namespace") or "default").strip()
                    key = str(params.get("key") or "").strip()
                    if not key:
                        raise ValueError(
                            f"Execution graph state_delete node {node_id} requires key"
                        )
                    if preview_mode:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "namespace": namespace,
                            "key": key,
                            "deleted": False,
                            "would_delete": True,
                        }
                        continue
                    deleted = delete_agent_state(
                        db,
                        company_id=company_id,
                        agent_id=agent_id,
                        namespace=namespace,
                        key=key,
                    )
                    node_outputs[node_id] = {
                        "namespace": namespace,
                        "key": key,
                        "deleted": bool(deleted),
                    }
                    continue
                elif node_type == "transform":
                    nested_step = {
                        "type": "transform",
                        "values": params.get("values") or params,
                    }
                elif node_type == "condition":
                    try:
                        matched = compare_values(
                            params.get("left"),
                            str(params.get("operator") or "eq"),
                            params.get("right"),
                        )
                    except Exception as exc:
                        raise ValueError(
                            f"Execution graph condition node {node_id} failed: {exc}"
                        ) from exc
                    node_outputs[node_id] = {"matched": bool(matched)}
                    continue
                elif node_type == "select":
                    items = params.get("items")
                    fields = params.get("fields")
                    if not isinstance(items, list):
                        raise ValueError(
                            f"Execution graph select node {node_id} requires a list"
                        )
                    if not isinstance(fields, list) or not fields:
                        raise ValueError(
                            f"Execution graph select node {node_id} requires fields"
                        )
                    clean_fields = [
                        str(field or "").strip()
                        for field in fields
                        if str(field or "").strip()
                    ][:50]
                    selected = []
                    for item in items[:1000]:
                        if isinstance(item, dict):
                            selected.append({
                                field: extract_data_path(item, field)
                                for field in clean_fields
                            })
                        else:
                            selected.append({"value": item})
                    node_outputs[node_id] = {
                        "items": selected,
                        "count": len(selected),
                    }
                    continue
                elif node_type == "filter":
                    items = params.get("items")
                    if not isinstance(items, list):
                        raise ValueError(
                            f"Execution graph filter node {node_id} requires a list"
                        )
                    path = str(params.get("path") or "").strip()
                    operator = str(params.get("operator") or "eq").strip().lower()
                    right = params.get("value")
                    filtered = []
                    for item in items[:1000]:
                        left = extract_data_path(item, path)
                        try:
                            matched = compare_values(left, operator, right)
                        except (TypeError, ValueError):
                            matched = False
                        if matched:
                            filtered.append(item)
                    node_outputs[node_id] = {
                        "items": filtered,
                        "count": len(filtered),
                    }
                    continue
                elif node_type == "aggregate":
                    items = params.get("items")
                    if not isinstance(items, list):
                        raise ValueError(
                            f"Execution graph aggregate node {node_id} requires a list"
                        )
                    operation = str(params.get("operation") or "count").strip().lower()
                    path = str(params.get("path") or "").strip()
                    if operation == "count":
                        value = len(items)
                    else:
                        values = []
                        for item in items[:1000]:
                            raw = extract_data_path(item, path)
                            if isinstance(raw, bool):
                                continue
                            if isinstance(raw, (int, float)):
                                values.append(float(raw))
                        if operation == "sum":
                            value = sum(values)
                        elif operation == "avg":
                            value = (sum(values) / len(values)) if values else None
                        elif operation == "min":
                            value = min(values) if values else None
                        elif operation == "max":
                            value = max(values) if values else None
                        else:
                            raise ValueError(
                                f"Unsupported execution graph aggregate operation: {operation}"
                            )
                    node_outputs[node_id] = {
                        "value": value,
                        "operation": operation,
                    }
                    continue
                elif node_type == "notify":
                    message = str(params.get("message") or node.get("label") or "").strip()
                    if not message:
                        raise ValueError(
                            f"Execution graph notify node {node_id} requires message"
                        )
                    title = str(params.get("title") or "Employee update").strip()[:255]
                    if preview_mode:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "notification": {
                                "title": title,
                                "message": message[:4000],
                                "severity": str(params.get("severity") or "info"),
                                "persisted": False,
                            },
                        }
                        continue
                    event, duplicate = _employee_notification_event(
                        db,
                        company_id=company_id,
                        agent_id=int(graph_agent_id or 0) or None,
                        run_id=run_id,
                        execution_key=str(state.get("_xvond_execution_key") or ""),
                        node_scope=node_scope,
                        title=title,
                        message=message,
                        severity=str(params.get("severity") or "info"),
                    )
                    node_outputs[node_id] = {
                        "notification": {
                            "title": event.title,
                            "message": event.message,
                            "severity": event.severity,
                            "event_id": event.id,
                            "duplicate": duplicate,
                        }
                    }
                    continue
                elif node_type == "await_event":
                    if is_resume_node and node_resume.get("event_received") is True:
                        payload = node_resume.get("event_payload")
                        if not isinstance(payload, dict):
                            payload = {}
                        node_outputs[node_id] = {
                            "event": str(node_resume.get("event_name") or ""),
                            "event_id": str(node_resume.get("event_id") or ""),
                            "payload": deepcopy(payload),
                            "received": True,
                        }
                        continue
                    event_name = str(params.get("event") or "").strip().lower()
                    if not event_name:
                        raise ValueError(
                            f"Execution graph await_event node {node_id} requires event"
                        )
                    raw_match = params.get("match")
                    match = raw_match if isinstance(raw_match, dict) else {}
                    if preview_mode:
                        payloads = (
                            state.get("_xvond_preview_event_payloads")
                            if isinstance(state.get("_xvond_preview_event_payloads"), dict)
                            else {}
                        )
                        simulated_payload = deepcopy(
                            payloads.get(node_scope)
                            if node_scope in payloads
                            else payloads.get(node_id, {})
                        )
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "event_name": event_name,
                            "match": deepcopy(match),
                            "payload": simulated_payload,
                            "received": False,
                            "would_wait": True,
                        }
                        continue
                    raise AutomationEventRequired(
                        event_name=event_name,
                        match=match,
                        workflow_step_index=step_index,
                        node_id=node_id,
                        node_outputs=node_outputs,
                        graph_resume={
                            "node_id": node_id,
                            "node_outputs": deepcopy(node_outputs),
                            "event_received": False,
                            "event_name": event_name,
                        },
                    )
                elif node_type == "wait":
                    if is_resume_node and node_resume.get("wait_completed") is True:
                        node_outputs[node_id] = {
                            "resumed": True,
                            "resume_at": str(node_resume.get("resume_at") or ""),
                        }
                        continue
                    resume_at = _wait_resume_at(params)
                    if preview_mode:
                        node_outputs[node_id] = {
                            "preview": True,
                            "simulated": True,
                            "would_wait": True,
                            "resume_at": _trace_iso(resume_at),
                        }
                        continue
                    if resume_at <= _utcnow_naive():
                        node_outputs[node_id] = {
                            "resumed": True,
                            "resume_at": _trace_iso(resume_at),
                        }
                        continue
                    raise AutomationWaitRequired(
                        resume_at=resume_at,
                        workflow_step_index=step_index,
                        node_id=node_id,
                        node_outputs=node_outputs,
                        graph_resume={
                            "node_id": node_id,
                            "node_outputs": deepcopy(node_outputs),
                            "wait_completed": True,
                            "resume_at": _trace_iso(resume_at),
                        },
                    )
                elif node_type == "foreach":
                    items = params.get("items")
                    if not isinstance(items, list):
                        raise ValueError(
                            f"Execution graph foreach node {node_id} requires a list"
                        )
                    if len(items) > 100:
                        raise ValueError(
                            f"Execution graph foreach node {node_id} exceeds 100 items"
                        )
                    nested_depth = int(state.get("_xvond_nested_graph_depth") or 0) + 1
                    if nested_depth > MAX_NESTED_GRAPH_DEPTH:
                        raise ValueError(
                            f"Execution graph foreach node {node_id} exceeds nested depth "
                            f"{MAX_NESTED_GRAPH_DEPTH}"
                        )
                    nested_graph = normalize_execution_graph(
                        params.get("graph") or {}
                    )
                    if not nested_graph.get("nodes"):
                        raise ValueError(
                            f"Execution graph foreach node {node_id} requires a nested graph"
                        )

                    foreach_resume = (
                        node_resume.get("foreach")
                        if isinstance(node_resume, dict)
                        and isinstance(node_resume.get("foreach"), dict)
                        else None
                    )
                    results = []
                    start_index = 0
                    child_resume = None
                    if foreach_resume is not None:
                        saved_items_fingerprint = str(
                            foreach_resume.get("items_fingerprint") or ""
                        ).strip()
                        if (
                            not saved_items_fingerprint
                            or saved_items_fingerprint
                            != _checkpoint_fingerprint(items)
                        ):
                            raise ValueError(
                                f"Execution graph foreach node {node_id} items changed after approval checkpoint"
                            )
                        try:
                            start_index = int(foreach_resume.get("loop_index"))
                        except (TypeError, ValueError) as exc:
                            raise ValueError(
                                f"Execution graph foreach node {node_id} has an invalid resume index"
                            ) from exc
                        saved_results = foreach_resume.get("completed_results")
                        if not isinstance(saved_results, list):
                            raise ValueError(
                                f"Execution graph foreach node {node_id} has invalid resume results"
                            )
                        if start_index < 0 or start_index >= len(items):
                            raise ValueError(
                                f"Execution graph foreach node {node_id} resume index is out of range"
                            )
                        if len(saved_results) != start_index:
                            raise ValueError(
                                f"Execution graph foreach node {node_id} resume results do not match its index"
                            )
                        results = deepcopy(saved_results)
                        child_resume = foreach_resume.get("child_resume")
                        if not isinstance(child_resume, dict):
                            raise ValueError(
                                f"Execution graph foreach node {node_id} is missing its child checkpoint"
                            )

                    for loop_index in range(start_index, len(items)):
                        loop_item = items[loop_index]
                        nested_step_index = (
                            (step_index * 100000)
                            + (node_index * 1000)
                            + loop_index
                            + 1
                        )
                        nested_state = {
                            **state,
                            "_xvond_loop_item": loop_item,
                            "_xvond_loop_index": loop_index,
                            "_xvond_nested_graph_depth": nested_depth,
                            "_xvond_graph_path": f"{node_scope}[{loop_index}]",
                            "_xvond_retry_root_step_index": retry_root_step_index,
                            "_xvond_retry_root_state": deepcopy(retry_root_state),
                            "_xvond_retry_lineage": [
                                *deepcopy(retry_lineage),
                                {
                                    "node_id": node_id,
                                    "node_outputs": deepcopy(node_outputs),
                                    "loop_index": loop_index,
                                    "items_fingerprint": _checkpoint_fingerprint(items),
                                    "completed_results": deepcopy(results),
                                },
                            ],
                        }
                        if child_resume is not None and loop_index == start_index:
                            nested_state["_xvond_graph_resume"] = {
                                "workflow_step_index": nested_step_index,
                                **deepcopy(child_resume),
                            }
                        try:
                            nested_result = self.execute_step(
                                db,
                                company_id,
                                {
                                    "type": "graph",
                                    "agent_id": graph_agent_id,
                                    "graph": nested_graph,
                                },
                                nested_state,
                                run_id=run_id,
                                step_index=nested_step_index,
                            )
                        except AutomationEventRequired as event_wait:
                            child_checkpoint = deepcopy(
                                event_wait.graph_resume
                                if isinstance(event_wait.graph_resume, dict)
                                else {
                                    "node_id": event_wait.node_id,
                                    "node_outputs": event_wait.node_outputs,
                                    "event_received": False,
                                    "event_name": event_wait.event_name,
                                }
                            )
                            event_wait.workflow_step_index = int(step_index)
                            event_wait.node_outputs = deepcopy(node_outputs)
                            event_wait.graph_resume = {
                                "node_id": node_id,
                                "node_outputs": deepcopy(node_outputs),
                                "foreach": {
                                    "loop_index": loop_index,
                                    "items_fingerprint": _checkpoint_fingerprint(items),
                                    "completed_results": deepcopy(results),
                                    "child_resume": child_checkpoint,
                                },
                            }
                            raise
                        except AutomationWaitRequired as wait:
                            child_checkpoint = deepcopy(
                                wait.graph_resume
                                if isinstance(wait.graph_resume, dict)
                                else {
                                    "node_id": wait.node_id,
                                    "node_outputs": wait.node_outputs,
                                    "wait_completed": True,
                                    "resume_at": _trace_iso(wait.resume_at),
                                }
                            )
                            wait.workflow_step_index = int(step_index)
                            wait.node_outputs = deepcopy(node_outputs)
                            wait.graph_resume = {
                                "node_id": node_id,
                                "node_outputs": deepcopy(node_outputs),
                                "foreach": {
                                    "loop_index": loop_index,
                                    "items_fingerprint": _checkpoint_fingerprint(items),
                                    "completed_results": deepcopy(results),
                                    "child_resume": child_checkpoint,
                                },
                            }
                            raise
                        except AutomationApprovalRequired as approval:
                            child_checkpoint = deepcopy(
                                approval.graph_resume
                                if isinstance(approval.graph_resume, dict)
                                else {
                                    "node_id": approval.node_id,
                                    "node_outputs": approval.node_outputs,
                                }
                            )
                            approval.workflow_step_index = int(step_index)
                            approval.node_outputs = deepcopy(node_outputs)
                            approval.graph_resume = {
                                "node_id": node_id,
                                "node_outputs": deepcopy(node_outputs),
                                "foreach": {
                                    "loop_index": loop_index,
                                    "items_fingerprint": _checkpoint_fingerprint(items),
                                    "completed_results": deepcopy(results),
                                    "child_resume": child_checkpoint,
                                },
                            }
                            raise
                        except Exception as exc:
                            raise ValueError(
                                f"Execution graph foreach node {node_id} failed at item {loop_index}: {exc}"
                            ) from exc
                        results.append(nested_result)
                        child_resume = None
                    node_outputs[node_id] = {
                        "items": results,
                        "count": len(results),
                    }
                    continue
                else:
                    raise ValueError(f"Unsupported execution graph node type: {node_type}")

                try:
                    node_result = self.execute_step(
                        db,
                        company_id,
                        nested_step,
                        {
                            **state,
                            **{
                                key: value
                                for output in node_outputs.values()
                                if isinstance(output, dict)
                                for key, value in output.items()
                            },
                        },
                        run_id=run_id,
                        step_index=(step_index * 1000) + node_index + 1,
                    )
                except AutomationEventRequired as event_wait:
                    event_wait.workflow_step_index = int(step_index)
                    event_wait.node_id = node_id
                    event_wait.node_outputs = deepcopy(node_outputs)
                    event_wait.graph_resume = {
                        "node_id": node_id,
                        "node_outputs": deepcopy(node_outputs),
                        "event_received": False,
                        "event_name": event_wait.event_name,
                    }
                    raise
                except AutomationWaitRequired as wait:
                    wait.workflow_step_index = int(step_index)
                    wait.node_id = node_id
                    wait.node_outputs = deepcopy(node_outputs)
                    wait.graph_resume = {
                        "node_id": node_id,
                        "node_outputs": deepcopy(node_outputs),
                        "wait_completed": True,
                        "resume_at": _trace_iso(wait.resume_at),
                    }
                    raise
                except AutomationApprovalRequired as approval:
                    approval.workflow_step_index = int(step_index)
                    approval.node_id = node_id
                    approval.node_outputs = deepcopy(node_outputs)
                    if not approval.approval_scope:
                        approval.approval_scope = node_scope
                    approval.graph_resume = {
                        "node_id": node_id,
                        "node_outputs": deepcopy(node_outputs),
                    }
                    raise
                except Exception as exc:
                    raise ValueError(
                        f"Execution graph node {node_id} ({node_type}) failed: {exc}"
                    ) from exc
                node_outputs[node_id] = node_result if isinstance(node_result, dict) else {
                    "result": node_result
                }

            if waiting_for_resume_node:
                raise ValueError(
                    f"Execution graph resume node {resume_node_id} is unavailable"
                )
            return {
                "graph_outputs": node_outputs,
                "graph_last": node_outputs.get(str(nodes[-1].get("id") or "")),
            }

        if step_type == "transform":
            values = step.get("values") or {}
            if not isinstance(values, dict):
                raise ValueError("Transform values must be an object")
            return dict(values)

        if step_type == "condition":
            field = str(step.get("field") or "").strip()
            if not field:
                raise ValueError("Condition step requires field")
            expected = step.get("equals")
            actual = state.get(field)
            if actual != expected:
                raise ValueError(
                    f"Condition failed: {field} expected {expected!r}, got {actual!r}"
                )
            return {"condition_passed": True}

        if step_type == "ai":
            agent_id = step.get("agent_id")
            if not agent_id:
                raise ValueError("AI step requires agent_id")
            message = _compose_step_message(
                prompt=str(step.get("prompt") or step.get("label") or ""),
                context=step.get("context"),
            )
            response = agent_runtime.chat(
                db=db,
                company_id=company_id,
                agent_id=int(agent_id),
                message=message,
                commit=False,
                allow_tools=False,
                system_prompt_override=(
                    str(state.get("_xvond_preview_system_prompt"))
                    if state.get("_xvond_preview")
                    and state.get("_xvond_preview_system_prompt")
                    else None
                ),
            )
            return {
                "ai_response": response.get("response", {}).get("content", ""),
                "conversation_id": response.get("conversation_id"),
            }

        if step_type == "media_generation":
            explicit_context = step.get("context")
            if explicit_context is None:
                explicit_context = state.get("ai_response")
            prompt = _compose_step_message(
                prompt=str(step.get("prompt") or step.get("label") or ""),
                context=explicit_context,
            )
            asset = generate_image_asset(
                prompt=prompt,
                model=str(step.get("model") or "").strip() or None,
                size=str(step.get("size") or "1024x1024").strip(),
            )
            return {
                "media_url": asset["media_url"],
                "generated_media": {
                    "content_type": asset["content_type"],
                    "bytes": asset["bytes"],
                    "model": asset["model"],
                },
            }

        if step_type == "tool":
            agent_id = step.get("agent_id")
            tool_name = step.get("tool_name")
            if not agent_id or not tool_name:
                raise ValueError("Tool step requires agent_id and tool_name")
            result = tool_executor.execute(
                db=db,
                company_id=company_id,
                agent_id=int(agent_id),
                tool_name=str(tool_name),
                arguments=step.get("arguments") or {},
                approval_granted=bool(step.get("approval_granted", False)),
            )
            if not result.get("success"):
                raise ValueError(result.get("error") or "Tool execution failed")
            return {"tool_result": result.get("data")}

        if step_type == "scheduled_action":
            agent_id = step.get("agent_id")
            action_type = str(step.get("action_type") or "").strip()
            execution_key = str(state.get("_xvond_execution_key") or "").strip()
            if not agent_id or not action_type:
                raise ValueError("Scheduled action requires agent_id and action_type")
            if not execution_key:
                raise ValueError("Scheduled action requires a stable execution key")

            assignment = (
                db.query(AgentToolAssignment)
                .filter(
                    AgentToolAssignment.agent_id == int(agent_id),
                    AgentToolAssignment.tool_name == "action_request",
                    AgentToolAssignment.enabled.is_(True),
                )
                .first()
            )
            if assignment is None:
                raise ValueError("Scheduled action contract is not assigned to this employee")
            config = reveal_config(assignment.config) or {}
            action = (config.get("actions") or {}).get(action_type)
            if not isinstance(action, dict):
                raise ValueError("Scheduled action contract is missing")
            permission_mode = str(
                action.get("_xvond_permission_mode") or ""
            ).strip().lower()
            if permission_mode == "never":
                return {
                    "scheduled_action_result": {
                        "skipped": True,
                        "reason": "owner_permission_never",
                        "action_type": action_type,
                    }
                }
            if not action.get("enabled", True):
                raise ValueError("Scheduled action is not enabled")
            destination = action.get("destination") or {}
            if action.get("confirmation_required", True):
                approval_request_id = int(step.get("approval_request_id") or 0)
                approval = None
                if approval_request_id:
                    approval = (
                        db.query(ActionRequest)
                        .filter(
                            ActionRequest.id == approval_request_id,
                            ActionRequest.company_id == company_id,
                            ActionRequest.agent_id == int(agent_id),
                            ActionRequest.action_type == action_type,
                            ActionRequest.status == "approved",
                        )
                        .first()
                    )
                    if approval is not None:
                        expected_node_id = str(step.get("approval_node_id") or "").strip()
                        expected_scope = str(
                            step.get("approval_scope") or expected_node_id
                        ).strip()
                        if not _approval_checkpoint_matches(
                            approval,
                            node_id=expected_node_id,
                            approval_scope=expected_scope,
                        ):
                            approval = None
                if approval is None:
                    if not step.get("_xvond_graph_action"):
                        raise ValueError(
                            "Scheduled action requires automatic permission or a graph approval checkpoint"
                        )
                    raise AutomationApprovalRequired(
                        agent_id=int(agent_id),
                        action_type=action_type,
                        arguments=step.get("arguments") or {},
                        summary=str(
                            step.get("summary")
                            or action.get("description")
                            or action.get("label")
                            or action_type
                        ),
                        workflow_step_index=-1,
                        node_id=str(step.get("approval_node_id") or ""),
                        node_outputs={},
                        approval_scope=str(
                            step.get("approval_scope")
                            or step.get("approval_node_id")
                            or ""
                        ),
                    )

            details = {
                key: value
                for key, value in state.items()
                if not str(key).startswith("_xvond_")
                and key != "conversation_id"
            }
            details.update(step.get("arguments") or {})
            stable_key = f"{execution_key}:{step_index}"
            if step.get("_xvond_graph_action"):
                node_scope = str(
                    step.get("approval_scope")
                    or step.get("approval_node_id")
                    or ""
                ).strip()
                if not node_scope:
                    raise ValueError(
                        "Graph action requires a stable node scope for idempotency"
                    )
                scope_digest = sha256(node_scope.encode("utf-8")).hexdigest()[:24]
                stable_key = f"{stable_key}:graph:{scope_digest}"

            if (
                destination.get("type") == "xvond_internal"
                and destination.get("adapter") == "generic_capability"
            ):
                result = execute_generic_capability(
                    db,
                    company_id=company_id,
                    agent_id=int(agent_id),
                    action_type=action_type,
                    action_config=action,
                    details=details,
                    idempotency_key=stable_key,
                )
                return {"scheduled_action_result": result}

            if destination.get("type") == "integration":
                result = _integration_call(
                    db,
                    {"company_id": company_id, "agent_id": int(agent_id)},
                    action_type,
                    action,
                    {
                        "operation": "execute",
                        "action_type": action_type,
                        "details": details,
                        "summary": str(
                            step.get("summary")
                            or action.get("description")
                            or action.get("label")
                            or action_type
                        )[:2000],
                    },
                    "execute",
                    idempotency_key=stable_key,
                )
                if not result.success:
                    raise ValueError(result.error or "Scheduled integration action failed")
                return {
                    "scheduled_action_result": {
                        "runtime": "connected_integration",
                        "action_type": action_type,
                        "result": result.data or {},
                    }
                }

            raise ValueError(
                "Scheduled action destination is not supported for background execution"
            )

        if step_type == "webhook":
            integration_id = step.get("integration_id")
            if not integration_id:
                raise ValueError("Webhook step requires integration_id")
            integration = (
                db.query(CompanyIntegration)
                .filter(
                    CompanyIntegration.id == int(integration_id),
                    CompanyIntegration.company_id == company_id,
                    CompanyIntegration.integration_type == "webhook",
                    CompanyIntegration.enabled.is_(True),
                )
                .first()
            )
            if integration is None:
                raise ValueError("Enabled webhook integration not found")
            config = reveal_config(integration.config) or {}
            url = str(config.get("url") or "").strip()
            if not url:
                raise ValueError("Webhook URL is invalid")
            execution_key = str(state.get("_xvond_execution_key") or "").strip()
            idempotency_key = (
                f"{execution_key}:{step_index}"
                if execution_key
                else f"xvond-automation-{company_id}-{run_id}-{step_index}-v1"
            )
            headers = {
                "Idempotency-Key": idempotency_key,
                "X-Xvond-Idempotency-Key": idempotency_key,
            }
            if config.get("secret"):
                headers["X-Xvond-Webhook-Secret"] = str(config["secret"])
            result = safe_http_request(
                url=url,
                method="POST",
                json_data=state,
                headers=headers,
                timeout=15.0,
                max_response_bytes=250_000,
            )
            status = int(result.get("status_code") or 0)
            if not 200 <= status < 300:
                raise ValueError(f"Webhook returned HTTP {status}")
            return {
                "webhook_status": status,
                "webhook_idempotency_key": idempotency_key,
            }

        if step_type == "integration":
            raise ValueError(
                "Direct integration steps are configured through an agent tool "
                "or webhook integration"
            )

        raise ValueError(f"Unsupported automation step: {step_type}")


automation_runtime = AutomationRuntime()
