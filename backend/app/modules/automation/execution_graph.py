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
}


def _clean_id(value: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_")
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in {"_", "-"})
    return safe[:64]


def _bounded(value: Any, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def normalize_execution_graph(value: Any) -> dict:
    if not isinstance(value, dict):
        return {"version": GRAPH_VERSION, "nodes": []}

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
        if raw.get("label"):
            node["label"] = _bounded(raw.get("label"), 200)
        seen.add(node_id)
        nodes.append(node)
        if len(nodes) >= 50:
            break

    return {"version": GRAPH_VERSION, "nodes": nodes}


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
