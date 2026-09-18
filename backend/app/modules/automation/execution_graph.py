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
}


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
            event_name = _bounded(raw_trigger.get("event"), 120)
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


def graph_has_side_effect(graph: dict) -> bool:
    return any(
        isinstance(node, dict) and node.get("type") == "action"
        for node in (graph or {}).get("nodes") or []
    )


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
