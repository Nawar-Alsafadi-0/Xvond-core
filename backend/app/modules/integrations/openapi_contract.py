from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

import yaml

from backend.app.core.http_security import safe_http_request
from backend.app.modules.integrations.json_contract import sanitize_json_contract


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


def _normalized_auth_schemes(document: dict) -> list[dict]:
    components = document.get("components") if isinstance(document.get("components"), dict) else {}
    schemes = components.get("securitySchemes") if isinstance(components.get("securitySchemes"), dict) else {}
    result: list[dict] = []
    for raw_name, raw in list(schemes.items())[:20]:
        if not isinstance(raw, dict):
            continue
        name = str(raw_name or "").strip()[:80]
        scheme_type = str(raw.get("type") or "").strip().lower()
        item: dict[str, Any] | None = None
        if scheme_type == "apikey":
            location = str(raw.get("in") or "").strip().lower()
            param_name = str(raw.get("name") or "").strip()[:80]
            if location == "header" and param_name:
                item = {
                    "name": name,
                    "auth_type": "api_key_header",
                    "api_key_name": param_name,
                }
            elif location == "query" and param_name:
                item = {
                    "name": name,
                    "auth_type": "api_key_query",
                    "api_key_name": param_name,
                }
        elif scheme_type == "http":
            scheme = str(raw.get("scheme") or "").strip().lower()
            if scheme == "bearer":
                item = {"name": name, "auth_type": "bearer"}
            elif scheme == "basic":
                item = {"name": name, "auth_type": "basic"}
        elif scheme_type == "oauth2":
            flows = raw.get("flows") if isinstance(raw.get("flows"), dict) else {}
            supported_flows: list[dict] = []
            for flow_name in ("authorizationCode", "clientCredentials"):
                flow = flows.get(flow_name)
                if not isinstance(flow, dict):
                    continue
                authorization_url = str(flow.get("authorizationUrl") or "").strip()
                token_url = str(flow.get("tokenUrl") or "").strip()
                if flow_name == "authorizationCode" and not authorization_url:
                    continue
                if not token_url:
                    continue
                auth_parsed = urlparse(authorization_url) if authorization_url else None
                token_parsed = urlparse(token_url)
                if (
                    token_parsed.scheme.lower() != "https"
                    or not token_parsed.hostname
                    or token_parsed.username
                    or token_parsed.password
                ):
                    continue
                if auth_parsed is not None and (
                    auth_parsed.scheme.lower() != "https"
                    or not auth_parsed.hostname
                    or auth_parsed.username
                    or auth_parsed.password
                ):
                    continue
                scopes = flow.get("scopes") if isinstance(flow.get("scopes"), dict) else {}
                supported_flows.append({
                    "flow": "authorization_code" if flow_name == "authorizationCode" else "client_credentials",
                    "authorization_url": authorization_url,
                    "token_url": token_url,
                    "scopes": list(scopes.keys())[:50],
                })
            if supported_flows:
                item = {
                    "name": name,
                    "auth_type": "oauth",
                    "flows": supported_flows,
                }
        if item and item not in result:
            result.append(item)
    return result


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


_BODY_FIELD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")


def _local_schema_ref(document: dict, schema: dict) -> dict:
    """Resolve one bounded local OpenAPI/Swagger schema reference."""
    if not isinstance(schema, dict):
        return {}
    ref = str(schema.get("$ref") or "").strip()
    if not ref:
        return schema
    if not ref.startswith("#/") or len(ref) > 500:
        return {}
    current: Any = document
    for raw_part in ref[2:].split("/")[:12]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return {}
        current = current[part]
    return current if isinstance(current, dict) else {}


def _object_schema_parts(document: dict, schema: dict, *, depth: int = 0) -> tuple[dict, list[str]]:
    """Return bounded top-level object properties/required keys.

    The importer intentionally does not build an arbitrary JSON-schema engine.
    It only extracts the top-level request fields Xvond needs to collect before
    calling a discovered API. Local refs and shallow allOf composition are
    supported because they are common in generated OpenAPI contracts.
    """
    if depth > 4:
        return {}, []
    resolved = _local_schema_ref(document, schema)
    if not resolved:
        return {}, []

    properties: dict[str, dict] = {}
    required: list[str] = []

    for raw_key in resolved.get("required") or []:
        key = str(raw_key or "").strip()
        if _BODY_FIELD_RE.fullmatch(key) and key not in required:
            required.append(key)
        if len(required) >= 50:
            break

    raw_properties = resolved.get("properties")
    if isinstance(raw_properties, dict):
        for raw_key, raw_value in raw_properties.items():
            key = str(raw_key or "").strip()
            if not _BODY_FIELD_RE.fullmatch(key) or not isinstance(raw_value, dict):
                continue
            properties[key] = _local_schema_ref(document, raw_value) or raw_value
            if len(properties) >= 50:
                break

    all_of = resolved.get("allOf")
    if isinstance(all_of, list):
        for item in all_of[:8]:
            if not isinstance(item, dict):
                continue
            child_properties, child_required = _object_schema_parts(
                document,
                item,
                depth=depth + 1,
            )
            for key, value in child_properties.items():
                properties.setdefault(key, value)
                if len(properties) >= 50:
                    break
            for key in child_required:
                if key not in required:
                    required.append(key)
                if len(required) >= 50:
                    break

    return properties, required


def _multipart_schema_supported(document: dict, schema: dict) -> bool:
    if _schema_kind(document, schema) != "object":
        return False
    properties, _ = _object_schema_parts(document, schema)
    if not properties:
        return False
    allowed_types = {"string", "integer", "number", "boolean"}
    binary_fields = 0
    for raw in properties.values():
        value = _local_schema_ref(document, raw) or raw
        value_type = str(value.get("type") or "").strip().lower()
        value_format = str(value.get("format") or "").strip().lower()
        if value_format == "binary":
            if value_type != "string":
                return False
            binary_fields += 1
            if binary_fields > 5:
                return False
            continue
        if value_type not in allowed_types:
            return False
    return True


def _request_body_contract(
    document: dict,
    operation: dict,
    parameters: list,
) -> tuple[str, dict]:
    """Return one bounded request media contract.

    JSON and application/x-www-form-urlencoded are executable. Multipart is
    executable for declared scalar fields and bounded binary fields backed by
    tenant-owned Xvond file assets. Object/array multipart fields stay fail-closed.
    """
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        request_body = _local_schema_ref(document, request_body) or request_body
        content = request_body.get("content")
        if not isinstance(content, dict) or not content:
            return "unsupported", {}

        json_candidates: list[dict] = []
        if isinstance(content.get("application/json"), dict):
            json_candidates.append(content["application/json"])
        json_candidates.extend(
            value
            for media_type, value in content.items()
            if str(media_type or "").lower().endswith("+json")
            and isinstance(value, dict)
        )
        for media in json_candidates[:5]:
            schema = media.get("schema")
            if isinstance(schema, dict):
                return "json", schema

        form = content.get("application/x-www-form-urlencoded")
        if isinstance(form, dict):
            schema = form.get("schema")
            if isinstance(schema, dict):
                return "form", schema

        multipart = content.get("multipart/form-data")
        if isinstance(multipart, dict):
            schema = multipart.get("schema")
            if (
                isinstance(schema, dict)
                and _multipart_schema_supported(document, schema)
            ):
                return "multipart", schema

        # Never downgrade binary multipart, XML, arbitrary binary or unknown
        # body formats to JSON. That would silently execute the wrong provider call.
        return "unsupported", {}

    # Swagger 2.0 body/formData contracts.
    consumes = operation.get("consumes")
    if not isinstance(consumes, list):
        consumes = document.get("consumes")
    consumes = [
        str(item or "").strip().lower()
        for item in (consumes or [])
        if str(item or "").strip()
    ]

    for parameter in parameters[:100]:
        if not isinstance(parameter, dict):
            continue
        if str(parameter.get("in") or "").strip().lower() != "body":
            continue
        schema = parameter.get("schema")
        if not isinstance(schema, dict):
            return "unsupported", {}
        if consumes and not any(
            media == "application/json" or media.endswith("+json")
            for media in consumes
        ):
            return "unsupported", {}
        return "json", schema

    form_parameters = [
        parameter
        for parameter in parameters[:100]
        if isinstance(parameter, dict)
        and str(parameter.get("in") or "").strip().lower() == "formdata"
    ]
    if form_parameters:
        multipart_mode = bool(consumes and "multipart/form-data" in consumes)
        if consumes and not (
            "application/x-www-form-urlencoded" in consumes
            or multipart_mode
        ):
            return "unsupported", {}
        properties: dict[str, dict] = {}
        required: list[str] = []
        for parameter in form_parameters[:50]:
            key = str(parameter.get("name") or "").strip()
            if not _BODY_FIELD_RE.fullmatch(key):
                continue
            field_type = str(parameter.get("type") or "string").strip().lower()[:20]
            fmt = str(parameter.get("format") or "").strip().lower()[:40]
            if multipart_mode and field_type == "file":
                field_type = "string"
                fmt = "binary"
            if multipart_mode and (
                field_type not in {"string", "integer", "number", "boolean"}
                or (fmt == "binary" and field_type != "string")
            ):
                return "unsupported", {}
            field: dict[str, Any] = {"type": field_type}
            if fmt:
                field["format"] = fmt
            description = str(parameter.get("description") or "").strip()[:300]
            if description:
                field["description"] = description
            enum = parameter.get("enum")
            if isinstance(enum, list):
                bounded_enum = [
                    item
                    for item in enum[:20]
                    if isinstance(item, (str, int, float, bool)) or item is None
                ]
                if bounded_enum:
                    field["enum"] = bounded_enum
            properties[key] = field
            if parameter.get("required") is True:
                required.append(key)
        if not properties:
            return "unsupported", {}
        return ("multipart" if multipart_mode else "form"), {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    return "none", {}

def _openapi_json_contract(document: dict, schema: dict, *, _depth: int = 0) -> dict:
    if _depth > 4:
        return {}
    resolved = _local_schema_ref(document, schema) or schema
    if not isinstance(resolved, dict):
        return {}
    kind = _schema_kind(document, resolved)
    if kind not in {"object", "array", "string", "integer", "number", "boolean"}:
        return {}

    contract: dict[str, Any] = {"type": kind}
    fmt = str(resolved.get("format") or "").strip().lower()[:40]
    if fmt:
        contract["format"] = fmt
    enum = resolved.get("enum")
    if isinstance(enum, list):
        bounded_enum = [
            item for item in enum[:20]
            if isinstance(item, (str, int, float, bool)) or item is None
        ]
        if bounded_enum:
            contract["enum"] = bounded_enum

    if kind == "object":
        properties, required = _object_schema_parts(document, resolved)
        nested = {}
        for key, raw in list(properties.items())[:50]:
            child = _openapi_json_contract(document, raw, _depth=_depth + 1)
            if child:
                nested[key] = child
        if nested:
            contract["properties"] = nested
            required = [item for item in required if item in nested]
        if required:
            contract["required"] = required[:50]
    elif kind == "array":
        items = resolved.get("items")
        child = _openapi_json_contract(
            document, items, _depth=_depth + 1
        ) if isinstance(items, dict) else {}
        if not child:
            return {}
        contract["items"] = child
        contract["max_items"] = 100

    return sanitize_json_contract(contract)

def _json_field_metadata(document: dict, schema: dict) -> tuple[list[str], list[dict]]:
    properties, required = _object_schema_parts(document, schema)
    fields: list[dict] = []
    required_set = set(required)
    for key, raw in properties.items():
        value = _local_schema_ref(document, raw) or raw
        field: dict[str, Any] = {
            "key": key,
            "required": key in required_set,
            "type": str(value.get("type") or "string").strip().lower()[:20],
        }
        fmt = str(value.get("format") or "").strip().lower()[:40]
        if fmt:
            field["format"] = fmt
        description = str(value.get("description") or "").strip()[:300]
        if description:
            field["description"] = description
        enum = value.get("enum")
        if isinstance(enum, list):
            bounded_enum = [
                item for item in enum[:20]
                if isinstance(item, (str, int, float, bool)) or item is None
            ]
            if bounded_enum:
                field["enum"] = bounded_enum
        if _schema_kind(document, value) in {"object", "array"}:
            nested_contract = _openapi_json_contract(document, value)
            if nested_contract:
                field["schema"] = nested_contract
        fields.append(field)
        if len(fields) >= 50:
            break
    return required[:50], fields


def _success_response_schema(document: dict, operation: dict) -> tuple[str | None, dict]:
    """Return the first schema-bearing declared 2xx response, bounded and local-ref only."""
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None, {}

    candidates: list[tuple[int, str, dict]] = []
    for raw_status, raw_response in responses.items():
        status = str(raw_status or "").strip()
        if not re.fullmatch(r"2[0-9][0-9]", status):
            continue
        if not isinstance(raw_response, dict):
            continue
        candidates.append((int(status), status, raw_response))
    candidates.sort(key=lambda item: item[0])
    if not candidates:
        return None, {}

    fallback_status = candidates[0][1]
    for _, status, raw_response in candidates:
        response = _local_schema_ref(document, raw_response) or raw_response

        content = response.get("content")
        if isinstance(content, dict):
            media_candidates: list[dict] = []
            if isinstance(content.get("application/json"), dict):
                media_candidates.append(content["application/json"])
            media_candidates.extend(
                value
                for media_type, value in content.items()
                if str(media_type or "").lower().endswith("+json")
                and isinstance(value, dict)
            )
            for media in media_candidates[:5]:
                schema = media.get("schema")
                if isinstance(schema, dict):
                    return status, schema

        # Swagger 2.0 success responses expose the schema directly.
        schema = response.get("schema")
        if isinstance(schema, dict):
            return status, schema

    return fallback_status, {}


def _schema_kind(document: dict, schema: dict) -> str:
    resolved = _local_schema_ref(document, schema) or schema
    if not isinstance(resolved, dict) or not resolved:
        return "none"
    raw_type = str(resolved.get("type") or "").strip().lower()
    if raw_type in {"object", "array", "string", "integer", "number", "boolean"}:
        return raw_type
    if isinstance(resolved.get("properties"), dict) or isinstance(resolved.get("allOf"), list):
        return "object"
    return "unknown"


def _json_array_request_metadata(document: dict, schema: dict) -> dict:
    resolved = _local_schema_ref(document, schema) or schema
    if not isinstance(resolved, dict) or _schema_kind(document, resolved) != "array":
        return {}
    items = resolved.get("items")
    if not isinstance(items, dict):
        return {}
    item_kind = _schema_kind(document, items)
    if item_kind not in {"object", "string", "integer", "number", "boolean"}:
        return {}
    result: dict[str, Any] = {
        "array_item_kind": item_kind,
        "array_max_items": 100,
    }
    if item_kind == "object":
        required, fields = _json_field_metadata(document, items)
        result["required_array_item_fields"] = required
        result["array_item_fields"] = fields
    return result


def _response_metadata(document: dict, operation: dict) -> dict:
    status, schema = _success_response_schema(document, operation)
    if not status:
        return {}

    kind = _schema_kind(document, schema)
    result: dict[str, Any] = {
        "response_status": status,
        "response_kind": kind,
    }
    if kind == "object":
        _, fields = _json_field_metadata(document, schema)
        if fields:
            result["response_fields"] = fields[:25]
        return result

    if kind == "array":
        resolved = _local_schema_ref(document, schema) or schema
        items = resolved.get("items") if isinstance(resolved, dict) else None
        items = items if isinstance(items, dict) else {}
        item_kind = _schema_kind(document, items)
        result["response_item_kind"] = item_kind
        if item_kind == "object":
            _, fields = _json_field_metadata(document, items)
            if fields:
                result["response_item_fields"] = fields[:25]
        return result

    return result


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

            query_parameters = [
                item
                for item in parameters
                if isinstance(item, dict)
                and str(item.get("in") or "").strip().lower() == "query"
            ]
            has_query_parameters = bool(query_parameters)
            query_params = [
                str(item.get("name") or "").strip()
                for item in query_parameters
                if re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_.-]{0,63}",
                    str(item.get("name") or "").strip(),
                )
            ]
            required_query_params = [
                str(item.get("name") or "").strip()
                for item in query_parameters
                if item.get("required") is True
                and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_.-]{0,63}",
                    str(item.get("name") or "").strip(),
                )
            ]
            request_mode, request_schema = _request_body_contract(
                document,
                operation,
                parameters,
            )
            if request_mode == "unsupported":
                continue
            request_kind = _schema_kind(document, request_schema)
            if method == "GET" and request_mode in {"json", "form", "multipart"}:
                # GET request bodies are not portable enough for the generic
                # adapter; fail closed instead of manufacturing semantics.
                continue
            if request_mode in {"form", "multipart"} and request_kind != "object":
                continue
            if request_mode == "json" and request_kind not in {"object", "array"}:
                continue

            array_metadata = (
                _json_array_request_metadata(document, request_schema)
                if request_mode == "json" and request_kind == "array"
                else {}
            )
            if request_mode == "json" and request_kind == "array" and not array_metadata:
                continue

            required_body_fields, body_fields = (
                _json_field_metadata(document, request_schema)
                if request_kind == "object"
                else ([], [])
            )
            response_metadata = _response_metadata(document, operation)

            if method == "GET":
                input_mode = "query"
            elif request_mode == "json" and request_kind == "array":
                input_mode = "json_array"
            elif request_mode in {"json", "form", "multipart"}:
                input_mode = request_mode
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
                "query_params": list(dict.fromkeys(query_params)),
                "required_query_params": list(dict.fromkeys(required_query_params)),
                "required_json_fields": required_body_fields if input_mode == "json" else [],
                "json_fields": body_fields if input_mode == "json" else [],
                "required_form_fields": required_body_fields if input_mode in {"form", "multipart"} else [],
                "form_fields": body_fields if input_mode in {"form", "multipart"} else [],
                **array_metadata,
                **response_metadata,
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
        "auth_schemes": _normalized_auth_schemes(document),
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


_OPENAPI_DISCOVERY_SUFFIXES = (
    "openapi.json",
    "swagger.json",
    "api/openapi.json",
    "openapi.yaml",
    "swagger.yaml",
)


def _https_authority(value: str) -> tuple[str, int] | None:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    return str(parsed.hostname).rstrip(".").lower(), int(parsed.port or 443)


def openapi_discovery_urls(base_url: str) -> list[str]:
    """Return bounded same-host candidate documentation URLs for a configured API."""

    raw = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("API base URL must be a plain public HTTPS URL")

    origin = f"https://{parsed.netloc}"
    roots = [raw]
    if origin.rstrip("/") != raw:
        roots.append(origin.rstrip("/"))

    result: list[str] = []
    for root in roots:
        for suffix in _OPENAPI_DISCOVERY_SUFFIXES:
            candidate = root.rstrip("/") + "/" + suffix
            if candidate not in result:
                result.append(candidate)
            if len(result) >= 10:
                return result
    return result


def discover_openapi_contract(base_url: str) -> dict:
    """Probe common OpenAPI locations on the configured host only."""

    configured_authority = _https_authority(base_url)
    if configured_authority is None:
        raise ValueError("API base URL must be a plain public HTTPS URL")
    attempts: list[str] = []

    for url in openapi_discovery_urls(base_url):
        attempts.append(url)
        try:
            result = safe_http_request(
                url=url,
                method="GET",
                headers={
                    "Accept": "application/json, application/yaml, application/x-yaml, text/yaml, text/plain",
                    "User-Agent": "Xvond-OpenAPI-Discovery/1.0",
                },
                timeout=8,
                max_response_bytes=MAX_OPENAPI_RESPONSE_BYTES,
            )
        except Exception:
            continue

        status = int(result.get("status_code") or 0)
        if not 200 <= status < 300 or result.get("truncated"):
            continue

        try:
            contract = parse_openapi_text(str(result.get("response") or ""))
        except ValueError:
            continue

        discovered_base = str(contract.get("base_url") or "").strip()
        if discovered_base:
            if _https_authority(discovered_base) != configured_authority:
                # Discovery must never silently move execution to another
                # host or port.
                contract["base_url"] = None

        return {
            **contract,
            "discovery_url": url,
            "attempted_urls": attempts,
        }

    raise ValueError("No OpenAPI/Swagger contract was found on the configured API host")
