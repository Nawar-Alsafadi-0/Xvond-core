from __future__ import annotations

import re
from typing import Any


MAX_JSON_CONTRACT_DEPTH = 4
MAX_JSON_CONTRACT_FIELDS = 50
MAX_JSON_CONTRACT_ITEMS = 100

_FIELD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_ALLOWED_TYPES = {"object", "array", "string", "integer", "number", "boolean"}


def sanitize_json_contract(value: Any, *, _depth: int = 0) -> dict:
    """Return a bounded declarative JSON shape or an empty dict when invalid."""
    if _depth > MAX_JSON_CONTRACT_DEPTH or not isinstance(value, dict):
        return {}

    value_type = str(value.get("type") or "").strip().lower()
    if value_type not in _ALLOWED_TYPES:
        return {}

    result: dict[str, Any] = {"type": value_type}
    fmt = str(value.get("format") or "").strip().lower()[:40]
    if fmt:
        result["format"] = fmt
    enum = value.get("enum")
    if isinstance(enum, list):
        bounded_enum = [
            item
            for item in enum[:20]
            if isinstance(item, (str, int, float, bool)) or item is None
        ]
        if bounded_enum:
            result["enum"] = bounded_enum

    if value_type == "object":
        raw_properties = value.get("properties")
        if raw_properties is not None and not isinstance(raw_properties, dict):
            return {}
        properties: dict[str, dict] = {}
        for raw_key, raw_schema in list((raw_properties or {}).items())[:MAX_JSON_CONTRACT_FIELDS]:
            key = str(raw_key or "").strip()
            if not _FIELD_RE.fullmatch(key):
                continue
            child = sanitize_json_contract(raw_schema, _depth=_depth + 1)
            if child:
                properties[key] = child
        required = [
            str(item).strip()
            for item in (value.get("required") or [])
            if _FIELD_RE.fullmatch(str(item or "").strip())
        ][:MAX_JSON_CONTRACT_FIELDS]
        if properties:
            result["properties"] = properties
            required = [item for item in required if item in properties]
        if required:
            result["required"] = list(dict.fromkeys(required))
        return result

    if value_type == "array":
        item_contract = sanitize_json_contract(value.get("items"), _depth=_depth + 1)
        if not item_contract:
            return {}
        try:
            max_items = int(value.get("max_items") or MAX_JSON_CONTRACT_ITEMS)
        except (TypeError, ValueError):
            max_items = MAX_JSON_CONTRACT_ITEMS
        result["max_items"] = max(1, min(max_items, MAX_JSON_CONTRACT_ITEMS))
        result["items"] = item_contract
        return result

    return result


def _shape_free_json(value: Any, *, path: str, _depth: int = 0) -> Any:
    if _depth > MAX_JSON_CONTRACT_DEPTH:
        raise ValueError(f"{path} exceeds maximum JSON depth")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        if len(value) > MAX_JSON_CONTRACT_ITEMS:
            raise ValueError(
                f"{path} accepts at most {MAX_JSON_CONTRACT_ITEMS} items"
            )
        return [
            _shape_free_json(item, path=f"{path}[{index}]", _depth=_depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        if len(value) > MAX_JSON_CONTRACT_FIELDS:
            raise ValueError(
                f"{path} accepts at most {MAX_JSON_CONTRACT_FIELDS} fields"
            )
        return {
            str(key): _shape_free_json(
                item,
                path=f"{path}.{key}",
                _depth=_depth + 1,
            )
            for key, item in value.items()
            if isinstance(key, str) and len(key) <= 64
        }
    raise ValueError(f"{path} contains an unsupported JSON value")


def shape_json_value(value: Any, contract: dict, *, path: str = "$") -> Any:
    """Validate and shape a JSON value using the bounded declarative contract."""
    contract = sanitize_json_contract(contract)
    if not contract:
        raise ValueError(f"{path} has an invalid JSON contract")

    value_type = contract["type"]
    if value_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = contract.get("properties") or {}
        shaped = (
            {
                key: shape_json_value(value[key], child, path=f"{path}.{key}")
                for key, child in properties.items()
                if key in value
            }
            if properties
            else _shape_free_json(value, path=path)
        )
        missing = [
            key
            for key in contract.get("required") or []
            if key not in value or value.get(key) in (None, "")
        ]
        if missing:
            raise ValueError(
                f"{path} is missing required field(s): " + ", ".join(missing)
            )
        return shaped

    if value_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        max_items = int(contract.get("max_items") or MAX_JSON_CONTRACT_ITEMS)
        if len(value) > max_items:
            raise ValueError(f"{path} accepts at most {max_items} items")
        item_contract = contract["items"]
        return [
            shape_json_value(item, item_contract, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]

    if value_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
    elif value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
    elif value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be a number")
    elif value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")

    if "enum" in contract and value not in contract["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    return value
