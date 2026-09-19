from __future__ import annotations

from copy import deepcopy
from typing import Any

GRAPH_VERSION = 1
ALLOWED_GRAPH_NODE_TYPES = {
    "ai",
    "media",
    "action",
    "http_get_json",
    "transform",
    "condition",
    "notify",
    "foreach",
    "select",
    "filter",
    "aggregate",
    "web_fetch",
    "browser",
    "state_read",
    "state_write",
    "state_delete",
}

GRAPH_COMPARE_OPERATORS = {"eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"}
GRAPH_AGGREGATE_OPERATIONS = {"count", "sum", "avg", "min", "max"}
BROWSER_ACTION_OPERATIONS = {
    "goto",
    "wait_for",
    "extract_text",
    "extract_attribute",
    "extract_html",
    "click",
    "fill",
    "press",
    "select",
}
MAX_GRAPH_VALIDATION_DEPTH = 2
MAX_BROWSER_ACTIONS = 30


def _clean_id(value: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_")
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in {"_", "-"})
    return safe[:64]


def _bounded(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def normalize_execution_graph(value: Any) -> dict:
    if not isinstance(value, dict):
        return {"version": GRAPH_VERSION, "trigger": {"type": "manual"}, "nodes": []}

    raw_trigger = value.get("trigger")
    if isinstance(raw_trigger, dict):
        trigger_type = str(raw_trigger.get("type") or "manual").strip().lower()
        if trigger_type not in {"manual", "schedule", "webhook", "event"}:
            trigger_type = "manual"
        trigger = {"type": trigger_type}
        if trigger_type == "event":
            event_name = _bounded(raw_trigger.get("event"), 120).lower()
            if event_name:
                trigger["event"] = event_name
        if trigger_type == "schedule" and isinstance(raw_trigger.get("schedule"), dict):
            trigger["schedule"] = deepcopy(raw_trigger.get("schedule"))
    else:
        trigger = {"type": "manual"}

    nodes: list[dict] = []
    seen: set[str] = set()
    for raw in value.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        node_id = _clean_id(raw.get("id"))
        node_type = str(raw.get("type") or "").strip().lower()
        if not node_id or node_id in seen or node_type not in ALLOWED_GRAPH_NODE_TYPES:
            continue

        deps: list[str] = []
        for dep in raw.get("depends_on") or []:
            dep_id = _clean_id(dep)
            if dep_id and dep_id in seen and dep_id not in deps:
                deps.append(dep_id)

        params = raw.get("params")
        if not isinstance(params, dict):
            params = {}

        node = {
            "id": node_id,
            "type": node_type,
            "depends_on": deps,
            "params": deepcopy(params),
        }
        if "when" in raw:
            node["when"] = deepcopy(raw.get("when"))
        if raw.get("label"):
            node["label"] = _bounded(raw.get("label"), 200)
        seen.add(node_id)
        nodes.append(node)
        if len(nodes) >= 50:
            break

    return {"version": GRAPH_VERSION, "trigger": trigger, "nodes": nodes}



def _graph_node_references(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str) and value.startswith("$nodes."):
        parts = value.split(".")
        if len(parts) >= 2:
            node_id = _clean_id(parts[1])
            if node_id:
                refs.add(node_id)
        return refs
    if isinstance(value, dict):
        for item in value.values():
            refs.update(_graph_node_references(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_graph_node_references(item))
    return refs


def graph_contract_errors(
    value: Any,
    *,
    graph_agent_id: int | None = None,
    _depth: int = 0,
) -> list[str]:
    """Return structural runtime blockers for a normalized execution graph.

    The compiler already normalizes graph shape, but launch readiness must not
    rely on runtime exceptions to discover missing node parameters, invalid
    operations, or references to nodes that have not produced output yet.
    """

    if _depth > MAX_GRAPH_VALIDATION_DEPTH:
        return [f"execution graph exceeds nested depth {MAX_GRAPH_VALIDATION_DEPTH}"]

    graph = normalize_execution_graph(value)
    errors: list[str] = []
    previous_ids: set[str] = set()

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip()
        node_type = str(node.get("type") or "").strip().lower()
        params = node.get("params") if isinstance(node.get("params"), dict) else {}
        label = str(node.get("label") or "").strip()

        refs = _graph_node_references(params)
        if "when" in node:
            refs.update(_graph_node_references(node.get("when")))
        unavailable_refs = sorted(ref for ref in refs if ref not in previous_ids)
        if unavailable_refs:
            errors.append(
                f"{node_id}: references unavailable node output(s): "
                + ", ".join(unavailable_refs)
            )

        if node_type in {"ai", "media"}:
            if not str(params.get("prompt") or label).strip():
                errors.append(f"{node_id}: {node_type} node requires prompt or label")

        elif node_type == "action":
            if not str(params.get("action_type") or "").strip():
                errors.append(f"{node_id}: action node requires action_type")
            if not graph_agent_id and not params.get("agent_id"):
                errors.append(f"{node_id}: action node requires agent_id")

        elif node_type in {"http_get_json", "web_fetch"}:
            if not str(params.get("url") or "").strip():
                errors.append(f"{node_id}: {node_type} node requires url")

        elif node_type == "browser":
            if not str(params.get("url") or "").strip():
                errors.append(f"{node_id}: browser node requires url")
            actions = params.get("actions")
            if not isinstance(actions, list):
                errors.append(f"{node_id}: browser actions must be a list")
            else:
                if len(actions) > MAX_BROWSER_ACTIONS:
                    errors.append(
                        f"{node_id}: browser actions exceed {MAX_BROWSER_ACTIONS}"
                    )
                for index, action in enumerate(actions):
                    if not isinstance(action, dict):
                        errors.append(
                            f"{node_id}: browser action {index} must be an object"
                        )
                        continue
                    op = str(action.get("op") or "").strip().lower()
                    if op not in BROWSER_ACTION_OPERATIONS:
                        errors.append(
                            f"{node_id}: browser action {index} has unsupported operation"
                        )
                        continue
                    if op == "goto" and not str(action.get("url") or "").strip():
                        errors.append(
                            f"{node_id}: browser action {index} goto requires url"
                        )
                    if op in {
                        "wait_for",
                        "extract_text",
                        "extract_attribute",
                        "extract_html",
                        "click",
                        "fill",
                        "press",
                        "select",
                    } and not any(
                        str(action.get(key) or "").strip()
                        for key in ("selector", "text", "role")
                    ):
                        errors.append(
                            f"{node_id}: browser action {index} requires a locator"
                        )
                    if op == "extract_attribute" and not str(
                        action.get("attribute") or ""
                    ).strip():
                        errors.append(
                            f"{node_id}: browser action {index} requires attribute"
                        )
                    if op in {"fill", "select"} and not isinstance(
                        action.get("value"), (str, int, float)
                    ):
                        errors.append(
                            f"{node_id}: browser action {index} requires scalar value"
                        )
                    if op == "press" and not str(action.get("key") or "").strip():
                        errors.append(
                            f"{node_id}: browser action {index} requires key"
                        )

        elif node_type in {"state_read", "state_write", "state_delete"}:
            if not graph_agent_id and not params.get("agent_id"):
                errors.append(f"{node_id}: {node_type} node requires agent_id")
            if not str(params.get("key") or "").strip():
                errors.append(f"{node_id}: {node_type} node requires key")

        elif node_type == "condition":
            operator = str(params.get("operator") or "eq").strip().lower()
            if operator not in GRAPH_COMPARE_OPERATORS:
                errors.append(f"{node_id}: unsupported condition operator")

        elif node_type == "select":
            if "items" not in params:
                errors.append(f"{node_id}: select node requires items")
            fields = params.get("fields")
            if not isinstance(fields, list) or not any(
                str(item or "").strip() for item in fields
            ):
                errors.append(f"{node_id}: select node requires fields")

        elif node_type == "filter":
            if "items" not in params:
                errors.append(f"{node_id}: filter node requires items")
            operator = str(params.get("operator") or "eq").strip().lower()
            if operator not in GRAPH_COMPARE_OPERATORS:
                errors.append(f"{node_id}: unsupported filter operator")

        elif node_type == "aggregate":
            if "items" not in params:
                errors.append(f"{node_id}: aggregate node requires items")
            operation = str(params.get("operation") or "count").strip().lower()
            if operation not in GRAPH_AGGREGATE_OPERATIONS:
                errors.append(f"{node_id}: unsupported aggregate operation")

        elif node_type == "notify":
            if not str(params.get("message") or label).strip():
                errors.append(f"{node_id}: notify node requires message or label")

        elif node_type == "foreach":
            if "items" not in params:
                errors.append(f"{node_id}: foreach node requires items")
            nested = params.get("graph")
            if not isinstance(nested, dict):
                errors.append(f"{node_id}: foreach node requires nested graph")
            else:
                nested_graph = normalize_execution_graph(nested)
                if not nested_graph.get("nodes"):
                    errors.append(f"{node_id}: foreach node requires nested graph nodes")
                else:
                    nested_errors = graph_contract_errors(
                        nested_graph,
                        graph_agent_id=graph_agent_id,
                        _depth=_depth + 1,
                    )
                    errors.extend(
                        f"{node_id}/{item}" for item in nested_errors
                    )

        previous_ids.add(node_id)
        if len(errors) >= 50:
            break

    return errors[:50]


def graph_has_side_effect(graph: dict) -> bool:
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "").strip().lower()
        if node_type == "action":
            return True
        if node_type == "browser":
            actions = (node.get("params") or {}).get("actions") or []
            if any(
                isinstance(action, dict)
                and str(action.get("op") or "").strip().lower()
                in {"click", "fill", "press", "select"}
                for action in actions
            ):
                return True
        if node_type == "foreach":
            nested = (node.get("params") or {}).get("graph")
            if isinstance(nested, dict) and graph_has_side_effect(
                normalize_execution_graph(nested)
            ):
                return True
    return False


def graph_node_ids(graph: dict) -> list[str]:
    return [
        str(node.get("id"))
        for node in (graph or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("id")
    ]



def resolve_graph_value(value: Any, *, state: dict, node_outputs: dict) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        path = value[1:].split(".")
        if not path:
            return value
        if path[0] == "input":
            current: Any = state
            path = path[1:]
        elif path[0] == "item":
            current = state.get("_xvond_loop_item")
            path = path[1:]
        elif path[0] == "index":
            current = state.get("_xvond_loop_index")
            path = path[1:]
        elif path[0] == "nodes" and len(path) >= 2:
            current = node_outputs.get(path[1])
            path = path[2:]
        else:
            return value
        for key in path:
            if isinstance(current, dict):
                current = current.get(key)
            else:
                return None
        return current
    if isinstance(value, dict):
        return {
            key: resolve_graph_value(item, state=state, node_outputs=node_outputs)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_graph_value(item, state=state, node_outputs=node_outputs)
            for item in value
        ]
    return value



def graph_action_types(graph: dict) -> list[str]:
    result: list[str] = []

    def visit(current: dict) -> None:
        for node in (current or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "action":
                action_type = str((node.get("params") or {}).get("action_type") or "").strip()
                if action_type and action_type not in result:
                    result.append(action_type)
            elif node.get("type") == "foreach":
                nested = (node.get("params") or {}).get("graph")
                if isinstance(nested, dict):
                    visit(normalize_execution_graph(nested))

    visit(normalize_execution_graph(graph or {}))
    return result



def extract_data_path(value: Any, path: str | None) -> Any:
    current = value
    clean_path = str(path or "").strip()
    if not clean_path:
        return current
    for part in clean_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def compare_values(left: Any, operator: str, right: Any) -> bool:
    op = str(operator or "eq").strip().lower()
    if op == "eq":
        return left == right
    if op == "neq":
        return left != right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "contains":
        return right in left if left is not None else False
    if op == "in":
        return left in right if right is not None else False
    raise ValueError(f"Unsupported comparison operator: {op}")



def graph_nested_action_types(graph: dict) -> list[str]:
    result: list[str] = []

    def visit(current: dict, depth: int) -> None:
        for node in (current or {}).get("nodes") or []:
            if not isinstance(node, dict):
                continue
            if node.get("type") == "action" and depth > 0:
                action_type = str((node.get("params") or {}).get("action_type") or "").strip()
                if action_type and action_type not in result:
                    result.append(action_type)
            elif node.get("type") == "foreach":
                nested = (node.get("params") or {}).get("graph")
                if isinstance(nested, dict):
                    visit(normalize_execution_graph(nested), depth + 1)

    visit(normalize_execution_graph(graph or {}), 0)
    return result
