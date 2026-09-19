from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import yaml

from backend.app.core.http_security import safe_http_request


MAX_OPENAPI_RESPONSE_BYTES = 1_000_000
MAX_OPENAPI_OPERATIONS = 100
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PATH_PARAM_RE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]{0,63})}")
_OPERATION_NAME_RE = re.compile(r"[^a-z0-9]+")


def _operation_name(operation_id: Any, method: str, path: str, used: set[str]) -> str:
    raw = str(operation_id or "").strip()
    if raw:
        raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw).lower()
        base = _OPERATION_NAME_RE.sub("_", raw).strip("_")[:80]
    else:
        base = _OPERATION_NAME_RE.sub("_", f"{method}_{path}").strip("_")[:80]
    if not base or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,79}", base):
        base = "operation"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base[:72]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _static_https_server_url(document: dict) -> str | None:
    servers = document.get("servers")
    if not isinstance(servers, list):
        return None
    for item in servers[:10]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip().rstrip("/")
        if not url or "{" in url or "}" in url:
            continue
        parsed = urlparse(url)
        if parsed.scheme.lower() == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return url
    return None


def normalize_openapi_document(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("OpenAPI document must be an object")
    if not str(document.get("openapi") or document.get("swagger") or "").strip():
        raise ValueError("Document is not an OpenAPI/Swagger contract")

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("OpenAPI contract has no paths")

    operations: dict[str, dict] = {}
    used: set[str] = set()

    for raw_path, path_item in paths.items():
        path = str(raw_path or "").strip()
        if (
            not path.startswith("/")
            or path.startswith("//")
            or len(path) > 500
            or not isinstance(path_item, dict)
        ):
            continue

        placeholders = re.findall(r"{([^{}]+)}", path)
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) for name in placeholders):
            continue

        inherited_parameters = path_item.get("parameters")
        inherited_parameters = inherited_parameters if isinstance(inherited_parameters, list) else []

        for raw_method, operation in path_item.items():
            method = str(raw_method or "").strip().upper()
            if method not in _ALLOWED_METHODS or not isinstance(operation, dict):
                continue

            parameters = [*inherited_parameters]
            if isinstance(operation.get("parameters"), list):
                parameters.extend(operation["parameters"])

            has_query_parameters = any(
                isinstance(item, dict)
                and str(item.get("in") or "").strip().lower() == "query"
                for item in parameters
            )
            has_request_body = isinstance(operation.get("requestBody"), dict)

            if method == "GET":
                input_mode = "query"
            elif has_request_body:
                input_mode = "json"
            elif has_query_parameters:
                input_mode = "query"
            else:
                input_mode = "none"

            name = _operation_name(
                operation.get("operationId"),
                method,
                path,
                used,
            )
            operations[name] = {
                "method": method,
                "endpoint": path,
                "input_mode": input_mode,
                "timeout": 15,
                "path_params": list(dict.fromkeys(placeholders)),
                "description": str(
                    operation.get("summary")
                    or operation.get("description")
                    or ""
                ).strip()[:500],
            }
            if len(operations) >= MAX_OPENAPI_OPERATIONS:
                break
        if len(operations) >= MAX_OPENAPI_OPERATIONS:
            break

    if not operations:
        raise ValueError("OpenAPI contract has no supported HTTP operations")

    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    return {
        "version": 1,
        "title": str(info.get("title") or "Imported API").strip()[:200],
        "openapi_version": str(document.get("openapi") or document.get("swagger") or "").strip()[:40],
        "base_url": _static_https_server_url(document),
        "operations": operations,
    }


def parse_openapi_text(value: str) -> dict:
    text = str(value or "")
    if not text.strip():
        raise ValueError("OpenAPI document is empty")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError("OpenAPI document is not valid JSON or YAML") from exc
    if not isinstance(parsed, dict):
        raise ValueError("OpenAPI document must decode to an object")
    return normalize_openapi_document(parsed)


def fetch_openapi_contract(url: str) -> dict:
    result = safe_http_request(
        url=str(url or "").strip(),
        method="GET",
        headers={
            "Accept": "application/json, application/yaml, application/x-yaml, text/yaml, text/plain",
            "User-Agent": "Xvond-OpenAPI-Importer/1.0",
        },
        timeout=15,
        max_response_bytes=MAX_OPENAPI_RESPONSE_BYTES,
    )
    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        raise ValueError(f"OpenAPI URL returned HTTP {status}")
    if result.get("truncated"):
        raise ValueError("OpenAPI document is too large")
    return parse_openapi_text(str(result.get("response") or ""))
