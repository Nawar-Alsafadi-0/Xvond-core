from datetime import UTC, datetime

from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.config_secrets import reveal_config
from backend.app.core.http_security import safe_http_request
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.execution_graph import (
    compare_values,
    extract_data_path,
    normalize_execution_graph,
    resolve_graph_value,
)
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.media.generated_media import generate_image_asset
from backend.app.modules.tools.executor import tool_executor
from backend.app.modules.tools.action_request import _integration_call
from backend.app.modules.tools.generic_capability_runtime import execute_generic_capability
from backend.app.modules.tools.models import AgentToolAssignment


def _utcnow_naive() -> datetime:
    """Return UTC without tzinfo for compatibility with existing naive DB timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def _billing_contract(workflow: AutomationWorkflow) -> tuple[str, str]:
    source = str((workflow.trigger_config or {}).get("_xvond_source") or "").strip()
    if source == "self_service_employee":
        return "ai_agents", "automation_runs"
    return "automation", "runs"


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

        original_input = dict(input_data or {})
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
        step_results = []

        try:
            for index, step in enumerate(workflow.steps or []):
                result = self.execute_step(
                    db,
                    company_id,
                    step,
                    state,
                    run_id=run_id,
                    step_index=index,
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

            run.status = "success"
            run.output_data = {"state": state, "steps": step_results}
            run.finished_at = _utcnow_naive()
            db.commit()
            db.refresh(run)
            return run
        except Exception as original_error:
            error_message = str(original_error)[:2000]
            failed_output = {"state": state, "steps": step_results}
            db.rollback()

            usage_recorded = True
            try:
                service_limits.record(
                    db,
                    company_id,
                    billing_service,
                    billing_metric,
                    quantity=1,
                    metadata={"workflow_id": workflow.id, "status": "failed"},
                )
            except Exception:
                # A concurrent run may have consumed the final quota slot after
                # the original transaction rolled back. Preserve the workflow
                # failure instead of masking it with an accounting race.
                usage_recorded = False
                db.rollback()

            failed_run = AutomationRun(
                company_id=company_id,
                workflow_id=workflow.id,
                status="failed",
                input_data=original_input,
                output_data={
                    **failed_output,
                    "usage_recorded": usage_recorded,
                },
                error_message=error_message,
                finished_at=_utcnow_naive(),
            )
            db.add(failed_run)
            db.commit()
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

            node_outputs: dict[str, dict] = {}
            graph_agent_id = step.get("agent_id")
            for node_index, node in enumerate(nodes):
                node_id = str(node.get("id") or "").strip()
                node_type = str(node.get("type") or "").strip().lower()
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

                nested_step: dict = {"type": node_type}
                if node_type == "ai":
                    nested_step = {
                        "type": "ai",
                        "agent_id": params.get("agent_id") or graph_agent_id,
                        "prompt": params.get("prompt") or node.get("label"),
                    }
                elif node_type == "media":
                    nested_step = {
                        "type": "media_generation",
                        "prompt": params.get("prompt") or node.get("label"),
                        "model": params.get("model"),
                        "size": params.get("size") or "1024x1024",
                    }
                elif node_type == "action":
                    nested_step = {
                        "type": "scheduled_action",
                        "agent_id": params.get("agent_id") or graph_agent_id,
                        "action_type": params.get("action_type"),
                        "arguments": params.get("arguments") or {},
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
                elif node_type == "transform":
                    nested_step = {
                        "type": "transform",
                        "values": params.get("values") or params,
                    }
                elif node_type == "condition":
                    matched = compare_values(
                        params.get("left"),
                        str(params.get("operator") or "eq"),
                        params.get("right"),
                    )
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
                    node_outputs[node_id] = {
                        "notification": {
                            "title": str(params.get("title") or "Employee update")[:200],
                            "message": message[:2000],
                        }
                    }
                    continue
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
                    nested_graph = normalize_execution_graph(
                        params.get("graph") or {}
                    )
                    if not nested_graph.get("nodes"):
                        raise ValueError(
                            f"Execution graph foreach node {node_id} requires a nested graph"
                        )
                    results = []
                    for loop_index, loop_item in enumerate(items):
                        nested_result = self.execute_step(
                            db,
                            company_id,
                            {
                                "type": "graph",
                                "agent_id": graph_agent_id,
                                "graph": nested_graph,
                            },
                            {
                                **state,
                                "_xvond_loop_item": loop_item,
                                "_xvond_loop_index": loop_index,
                            },
                            run_id=run_id,
                            step_index=(step_index * 100000)
                            + (node_index * 1000)
                            + loop_index
                            + 1,
                        )
                        results.append(nested_result)
                    node_outputs[node_id] = {
                        "items": results,
                        "count": len(results),
                    }
                    continue
                else:
                    raise ValueError(f"Unsupported execution graph node type: {node_type}")

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
                node_outputs[node_id] = node_result if isinstance(node_result, dict) else {
                    "result": node_result
                }

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
            prompt = str(step.get("prompt") or step.get("label") or "").strip()
            if not agent_id:
                raise ValueError("AI step requires agent_id")
            if not prompt:
                raise ValueError("AI step requires prompt")
            response = agent_runtime.chat(
                db=db,
                company_id=company_id,
                agent_id=int(agent_id),
                message=prompt,
                commit=False,
                allow_tools=False,
            )
            return {
                "ai_response": response.get("response", {}).get("content", ""),
                "conversation_id": response.get("conversation_id"),
            }

        if step_type == "media_generation":
            instruction = str(step.get("prompt") or step.get("label") or "").strip()
            generated_content = str(state.get("ai_response") or "").strip()
            prompt_parts = [item for item in (instruction, generated_content) if item]
            prompt = "\n\nGenerated content/context:\n".join(prompt_parts)
            if not prompt:
                raise ValueError("Media generation requires a prompt or prior AI output")
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
            if not isinstance(action, dict) or not action.get("enabled", True):
                raise ValueError("Scheduled action is not enabled")
            destination = action.get("destination") or {}
            if action.get("confirmation_required", True):
                raise ValueError(
                    "Scheduled action requires automatic permission before background execution"
                )

            details = {
                key: value
                for key, value in state.items()
                if not str(key).startswith("_xvond_")
                and key != "conversation_id"
            }
            details.update(step.get("arguments") or {})
            stable_key = f"{execution_key}:{step_index}"

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
