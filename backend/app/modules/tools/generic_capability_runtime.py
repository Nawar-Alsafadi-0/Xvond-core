from __future__ import annotations

import hashlib
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import IntegrityError

from backend.app.modules.customer_ops.models import NotificationEvent


MAX_HTTP_RESPONSE_BYTES = 1_000_000
MAX_RUNTIME_STEPS = 20
_ALLOWED_COMPARE_OPERATORS = {"lt", "lte", "gt", "gte", "eq", "neq"}


class GenericCapabilityRuntimeError(RuntimeError):
    pass


def _safe_event_key(value: str) -> str:
    text = str(value or "").strip()
    if len(text) <= 240:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"{text[:210]}:{digest}"


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validated_https_url(url: str, *, allowed_hosts: list[str]) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise GenericCapabilityRuntimeError("Generic HTTP runtime requires an HTTPS URL")
    if parsed.username or parsed.password:
        raise GenericCapabilityRuntimeError("Credentials are not allowed in runtime URLs")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
        raise GenericCapabilityRuntimeError("Local/internal hosts are not allowed")
    normalized_allowed = {
        str(item or "").strip().rstrip(".").lower()
        for item in (allowed_hosts or [])
        if str(item or "").strip()
    }
    if not normalized_allowed or host not in normalized_allowed:
        raise GenericCapabilityRuntimeError("Runtime URL host is not approved for this employee")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not _is_public_ip(host):
            raise GenericCapabilityRuntimeError("Private or reserved runtime IPs are not allowed")
    else:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise GenericCapabilityRuntimeError("Runtime host could not be resolved") from exc
        if not addresses or any(not _is_public_ip(address) for address in addresses):
            raise GenericCapabilityRuntimeError("Runtime host resolved to a non-public address")
    return parsed.geturl()


def _extract_path(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise GenericCapabilityRuntimeError(f"Runtime extract path was not found: {path}")
    return current


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator not in _ALLOWED_COMPARE_OPERATORS:
        raise GenericCapabilityRuntimeError("Unsupported runtime comparison")
    try:
        if operator == "lt":
            return left < right
        if operator == "lte":
            return left <= right
        if operator == "gt":
            return left > right
        if operator == "gte":
            return left >= right
        if operator == "eq":
            return left == right
        return left != right
    except TypeError as exc:
        raise GenericCapabilityRuntimeError("Runtime comparison values are incompatible") from exc


def _notification_event(
    db,
    *,
    company_id: int,
    agent_id: int,
    action_type: str,
    idempotency_key: str,
    step: dict,
) -> tuple[NotificationEvent, bool]:
    event_key = _safe_event_key(
        f"employee-runtime:{company_id}:{agent_id}:{action_type}:{idempotency_key}:{step['id']}"
    )
    existing = (
        db.query(NotificationEvent)
        .filter(
            NotificationEvent.company_id == company_id,
            NotificationEvent.event_key == event_key,
        )
        .first()
    )
    if existing is not None:
        return existing, True

    event = NotificationEvent(
        company_id=company_id,
        event_key=event_key,
        event_type="employee_runtime",
        severity="info",
        title=str(step.get("title") or "Employee update")[:255],
        message=str(step.get("message") or "")[:4000] or None,
        payload={
            "agent_id": agent_id,
            "action_type": action_type,
            "runtime_step": step.get("id"),
            "idempotency_key": idempotency_key,
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
                NotificationEvent.company_id == company_id,
                NotificationEvent.event_key == event_key,
            )
            .first()
        )
        if existing is None:
            raise
        return existing, True


def generic_capability_readiness(action_config: dict) -> dict:
    destination = action_config.get("destination") or {}
    plan = destination.get("execution_plan") or []
    if not isinstance(plan, list) or not plan:
        return {"ready": False, "reason": "execution_plan_required"}
    if len(plan) > MAX_RUNTIME_STEPS:
        return {"ready": False, "reason": "execution_plan_too_large"}
    allowed_hosts = destination.get("allowed_hosts") or []
    if any(isinstance(step, dict) and step.get("op") == "http_get_json" for step in plan):
        if not allowed_hosts:
            return {"ready": False, "reason": "approved_https_host_required"}
    return {"ready": True, "reason": None}


def execute_generic_capability(
    db,
    *,
    company_id: int,
    agent_id: int,
    action_type: str,
    action_config: dict,
    details: dict,
    idempotency_key: str,
) -> dict:
    readiness = generic_capability_readiness(action_config)
    if not readiness["ready"]:
        raise GenericCapabilityRuntimeError(str(readiness["reason"]))

    destination = action_config.get("destination") or {}
    plan = destination.get("execution_plan") or []
    allowed_hosts = list(destination.get("allowed_hosts") or [])
    values: dict[str, Any] = {}
    public_steps: dict[str, Any] = {}

    for step in plan[:MAX_RUNTIME_STEPS]:
        if not isinstance(step, dict):
            raise GenericCapabilityRuntimeError("Invalid runtime step")
        step_id = str(step.get("id") or "").strip()
        op = str(step.get("op") or "").strip()
        if not step_id:
            raise GenericCapabilityRuntimeError("Runtime step id is required")

        if op == "http_get_json":
            field = str(step.get("url_field") or "url").strip()
            url = _validated_https_url(str(details.get(field) or ""), allowed_hosts=allowed_hosts)
            response = httpx.get(
                url,
                timeout=10.0,
                follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "Xvond-Employee-Runtime/1.0"},
            )
            response.raise_for_status()
            if len(response.content) > MAX_HTTP_RESPONSE_BYTES:
                raise GenericCapabilityRuntimeError("Runtime HTTP response is too large")
            try:
                values[step_id] = response.json()
            except ValueError as exc:
                raise GenericCapabilityRuntimeError("Runtime HTTP response is not valid JSON") from exc
            public_steps[step_id] = {"fetched": True}
            continue

        if op == "extract":
            source = str(step.get("source") or "").strip()
            if source not in values:
                raise GenericCapabilityRuntimeError("Runtime extract source is unavailable")
            values[step_id] = _extract_path(values[source], str(step.get("path") or ""))
            public_steps[step_id] = {"extracted": True}
            continue

        if op == "compare":
            source = str(step.get("source") or "").strip()
            if source not in values:
                raise GenericCapabilityRuntimeError("Runtime comparison source is unavailable")
            if step.get("value_field"):
                right = details.get(str(step["value_field"]))
            else:
                right = step.get("value")
            values[step_id] = _compare(values[source], str(step.get("operator") or ""), right)
            public_steps[step_id] = {"matched": bool(values[step_id])}
            continue

        if op == "notify":
            when = str(step.get("when") or "").strip()
            if when and not bool(values.get(when)):
                values[step_id] = {"skipped": True}
                public_steps[step_id] = {"skipped": True}
                continue
            event, duplicate = _notification_event(
                db,
                company_id=company_id,
                agent_id=agent_id,
                action_type=action_type,
                idempotency_key=idempotency_key,
                step=step,
            )
            values[step_id] = {"notification_event_id": event.id, "duplicate": duplicate}
            public_steps[step_id] = dict(values[step_id])
            continue

        raise GenericCapabilityRuntimeError("Unsupported runtime operation")

    return {
        "runtime": "generic_capability",
        "steps": public_steps,
        "completed": True,
    }
