from __future__ import annotations

import base64
import re
from urllib.parse import urlencode


AUTH_TYPES = {"none", "bearer", "api_key_header", "api_key_query", "basic"}
_BLOCKED_HEADER_NAMES = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "cookie",
    "proxy-authorization",
    "proxy-authenticate",
}
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_QUERY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.~-]{0,63}$")
_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")


def normalized_http_api_auth_type(config: dict | None) -> str:
    value = config or {}
    raw = str(value.get("auth_type") or "").strip().lower()
    if not raw:
        raw = "bearer" if str(value.get("api_key") or "").strip() else "none"
    if raw not in AUTH_TYPES:
        raise ValueError("Unsupported API authentication type")
    return raw


def validate_http_api_auth_config(config: dict | None) -> str:
    value = config or {}
    auth_type = normalized_http_api_auth_type(value)
    api_key = str(value.get("api_key") or "").strip()

    if auth_type == "bearer":
        if not api_key:
            raise ValueError("API key/token is required for bearer authentication")
    elif auth_type == "api_key_header":
        if not api_key:
            raise ValueError("API key is required for header authentication")
        name = str(value.get("api_key_name") or "X-API-Key").strip()
        if not _HEADER_NAME_RE.fullmatch(name) or name.lower() in _BLOCKED_HEADER_NAMES:
            raise ValueError("API key header name is invalid")
        prefix = str(value.get("api_key_prefix") or "").strip()
        if prefix and not _PREFIX_RE.fullmatch(prefix):
            raise ValueError("API key header prefix is invalid")
    elif auth_type == "api_key_query":
        if not api_key:
            raise ValueError("API key is required for query authentication")
        name = str(value.get("api_key_name") or "api_key").strip()
        if not _QUERY_NAME_RE.fullmatch(name):
            raise ValueError("API key query parameter name is invalid")
    elif auth_type == "basic":
        if not str(value.get("username") or "").strip():
            raise ValueError("Username is required for basic authentication")
        if not str(value.get("password") or ""):
            raise ValueError("Password is required for basic authentication")

    return auth_type


def apply_http_api_auth(
    *,
    url: str,
    headers: dict | None,
    config: dict | None,
) -> tuple[str, dict]:
    value = config or {}
    auth_type = validate_http_api_auth_config(value)
    result_headers = {str(k): str(v) for k, v in (headers or {}).items()}

    if auth_type == "none":
        return url, result_headers
    if auth_type == "bearer":
        result_headers["Authorization"] = f"Bearer {value['api_key']}"
        return url, result_headers
    if auth_type == "api_key_header":
        name = str(value.get("api_key_name") or "X-API-Key").strip()
        prefix = str(value.get("api_key_prefix") or "").strip()
        token = str(value["api_key"])
        result_headers[name] = f"{prefix} {token}".strip() if prefix else token
        return url, result_headers
    if auth_type == "api_key_query":
        name = str(value.get("api_key_name") or "api_key").strip()
        separator = "&" if "?" in url else "?"
        return url + separator + urlencode([(name, str(value["api_key"]))]), result_headers

    token = base64.b64encode(
        f"{value['username']}:{value['password']}".encode("utf-8")
    ).decode("ascii")
    result_headers["Authorization"] = f"Basic {token}"
    return url, result_headers
