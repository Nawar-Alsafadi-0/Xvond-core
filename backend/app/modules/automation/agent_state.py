from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent


_STATE_ROOT = "_xvond_runtime_state"
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
MAX_NAMESPACES = 20
MAX_KEYS_PER_NAMESPACE = 200
MAX_VALUE_BYTES = 64_000


class AgentStateError(ValueError):
    pass


def _clean_name(value: str, *, label: str) -> str:
    clean = str(value or "").strip()
    if not _KEY_RE.fullmatch(clean):
        raise AgentStateError(
            f"{label} must be 1-120 characters using letters, numbers, dot, underscore, colon or dash"
        )
    return clean


def _bounded_json_value(value: Any) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AgentStateError("State value must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
        raise AgentStateError("State value exceeds 64 KB")
    return deepcopy(value)


def _locked_config(db, *, company_id: int, agent_id: int) -> AgentConfig:
    row = (
        db.query(AgentConfig)
        .join(AIAgent, AIAgent.id == AgentConfig.agent_id)
        .filter(
            AgentConfig.agent_id == int(agent_id),
            AIAgent.company_id == int(company_id),
        )
        .with_for_update()
        .first()
    )
    if row is None:
        raise AgentStateError("Agent state owner was not found")
    return row


def read_agent_state(
    db,
    *,
    company_id: int,
    agent_id: int,
    namespace: str,
    key: str,
    default: Any = None,
) -> Any:
    namespace = _clean_name(namespace, label="State namespace")
    key = _clean_name(key, label="State key")
    row = _locked_config(
        db,
        company_id=company_id,
        agent_id=agent_id,
    )
    settings = dict(row.settings or {})
    root = settings.get(_STATE_ROOT)
    if not isinstance(root, dict):
        return deepcopy(default)
    bucket = root.get(namespace)
    if not isinstance(bucket, dict):
        return deepcopy(default)
    return deepcopy(bucket.get(key, default))


def write_agent_state(
    db,
    *,
    company_id: int,
    agent_id: int,
    namespace: str,
    key: str,
    value: Any,
) -> Any:
    namespace = _clean_name(namespace, label="State namespace")
    key = _clean_name(key, label="State key")
    safe_value = _bounded_json_value(value)
    row = _locked_config(
        db,
        company_id=company_id,
        agent_id=agent_id,
    )
    settings = deepcopy(dict(row.settings or {}))
    root = settings.get(_STATE_ROOT)
    root = deepcopy(root) if isinstance(root, dict) else {}
    if namespace not in root and len(root) >= MAX_NAMESPACES:
        raise AgentStateError("Agent state namespace limit reached")
    bucket = root.get(namespace)
    bucket = deepcopy(bucket) if isinstance(bucket, dict) else {}
    if key not in bucket and len(bucket) >= MAX_KEYS_PER_NAMESPACE:
        raise AgentStateError("Agent state key limit reached")
    bucket[key] = safe_value
    root[namespace] = bucket
    settings[_STATE_ROOT] = root
    row.settings = settings
    db.flush()
    return deepcopy(safe_value)


def delete_agent_state(
    db,
    *,
    company_id: int,
    agent_id: int,
    namespace: str,
    key: str,
) -> bool:
    namespace = _clean_name(namespace, label="State namespace")
    key = _clean_name(key, label="State key")
    row = _locked_config(
        db,
        company_id=company_id,
        agent_id=agent_id,
    )
    settings = deepcopy(dict(row.settings or {}))
    root = settings.get(_STATE_ROOT)
    if not isinstance(root, dict):
        return False
    root = deepcopy(root)
    bucket = root.get(namespace)
    if not isinstance(bucket, dict) or key not in bucket:
        return False
    bucket = deepcopy(bucket)
    bucket.pop(key, None)
    if bucket:
        root[namespace] = bucket
    else:
        root.pop(namespace, None)
    if root:
        settings[_STATE_ROOT] = root
    else:
        settings.pop(_STATE_ROOT, None)
    row.settings = settings
    db.flush()
    return True
