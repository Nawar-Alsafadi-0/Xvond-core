from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from urllib.parse import quote, urlencode

from sqlalchemy import text

from backend.app.core.config_secrets import reveal_config
from backend.app.core.config.settings import settings
from backend.app.core.execution_claims import execution_claims
from backend.app.core.http_security import safe_http_request, validate_public_http_url
from backend.app.modules.ai_agent.models import AIMessage
from backend.app.modules.channels.handoff import activate_human_handoff
from backend.app.modules.channels.whatsapp_models import WhatsAppSession
from backend.app.modules.integrations.catalog import integration_validation_ready
from backend.app.modules.integrations.capability_discovery import oauth_client_credentials_token
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.integrations.http_api_auth import apply_http_api_auth
from backend.app.modules.integrations.oauth_authorization import (
    oauth_access_token_needs_refresh,
    oauth_token_timing,
    refresh_oauth_access_token,
)
from backend.app.modules.integrations.email_smtp import (
    EmailConnectorError,
    send_smtp_email,
)
from backend.app.modules.integrations.email_imap import (
    EmailReadConnectorError,
    read_imap_messages,
)
from backend.app.modules.integrations.google_calendar import (
    CalendarConnectorError,
    execute_google_calendar_operation,
)
from backend.app.modules.automation.event_outbox import enqueue_automation_event
from backend.app.modules.tools.base import AgentTool, ToolResult
from backend.app.modules.tools.business_models import ActionRequest, HumanHandoff


ACTIVE_SLOT_STATUSES = {
    "new",
    "confirmed",
    "processing",
    "in_progress",
    "executing",
}
CONFIRM_WORDS = {
    "yes", "y", "confirm", "confirmed", "ok", "okay", "sure",
    "نعم", "اي", "إي", "ايوه", "أيوه", "تمام", "موافق", "اكد", "أكد",
    "تأكيد", "ثبت", "اوكي", "أوكي",
}
NEGATIVE_PREFIXES = ("لا", "no", "not", "don't", "dont", "مو", "مش")


def _field_specs(action: dict) -> list[dict]:
    raw = action.get("fields")
    if not raw:
        raw = action.get("required_fields") or []
    result = []
    for item in raw:
        if isinstance(item, str):
            key = item.strip()
            if key:
                result.append(
                    {
                        "key": key,
                        "label": key.replace("_", " "),
                        "required": True,
                        "type": "text",
                    }
                )
            continue
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("name") or "").strip()
        if not key:
            continue
        result.append(
            {
                "key": key,
                "label": str(
                    item.get("label") or key.replace("_", " ")
                ).strip(),
                "required": bool(item.get("required", True)),
                "type": str(item.get("type") or "text").strip(),
                "role": str(item.get("role") or "").strip(),
            }
        )
    return result[:50]


def _missing(action: dict, details: dict) -> list[str]:
    return [
        field["key"]
        for field in _field_specs(action)
        if field.get("required")
        and not str(details.get(field["key"]) or "").strip()
    ]


def _summary(action: dict, details: dict, provided: str | None) -> str:
    if str(provided or "").strip():
        return str(provided).strip()[:2000]
    labels = {field["key"]: field["label"] for field in _field_specs(action)}
    parts = []
    for key, value in details.items():
        if key.startswith("_") or value in (None, "", [], {}):
            continue
        parts.append(f"{labels.get(key, key.replace('_', ' '))}: {value}")
    return " | ".join(parts)[:2000] or str(
        action.get("label") or action.get("name") or "Customer request"
    )


def _action(config: dict, action_type: str) -> dict | None:
    actions = config.get("actions") or {}
    if not isinstance(actions, dict):
        return None
    value = actions.get(action_type)
    if (
        not isinstance(value, dict)
        or not value.get("enabled", True)
        or str(value.get("_xvond_permission_mode") or "").strip().lower() == "never"
    ):
        return None
    return value


def _latest_user_message(db, conversation_id: int | None) -> str:
    if conversation_id is None:
        return ""
    row = (
        db.query(AIMessage)
        .filter(
            AIMessage.conversation_id == conversation_id,
            AIMessage.role == "user",
        )
        .order_by(AIMessage.id.desc())
        .first()
    )
    return (row.content or "").strip() if row else ""


def _customer_confirmed(message: str) -> bool:
    value = " ".join(
        str(message or "")
        .strip()
        .lower()
        .replace("؟", "")
        .replace("!", "")
        .replace(".", "")
        .split()
    )
    if not value:
        return False
    if value.startswith(NEGATIVE_PREFIXES):
        return False
    if value in {x.lower() for x in CONFIRM_WORDS}:
        return True
    return (
        any(token in value.split() for token in {x.lower() for x in CONFIRM_WORDS})
        and len(value.split()) <= 8
    )


def _schedule_fields(action: dict) -> tuple[str, str, str | None]:
    availability = action.get("availability") or {}
    date_field = str(availability.get("date_field") or "").strip()
    time_field = str(availability.get("time_field") or "").strip()
    resource_field = str(availability.get("resource_field") or "").strip() or None
    if not date_field or not time_field:
        for field in _field_specs(action):
            role = field.get("role")
            if not date_field and role == "date":
                date_field = field["key"]
            if not time_field and role == "time":
                time_field = field["key"]
    return date_field or "date", time_field or "time", resource_field


def _parse_hhmm(value: str) -> datetime:
    return datetime.strptime(value, "%H:%M")


def _internal_slots(
    db,
    context: dict,
    action_type: str,
    action: dict,
    details: dict,
) -> ToolResult:
    availability = action.get("availability") or {}
    schedule = availability.get("schedule") or {}
    date_field, time_field, resource_field = _schedule_fields(action)
    raw_date = str(details.get(date_field) or "").strip()
    if not raw_date:
        return ToolResult(
            success=False,
            error=f"{date_field} is required to check availability",
        )
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        return ToolResult(
            success=False,
            error=f"{date_field} must use YYYY-MM-DD",
        )

    weekdays = schedule.get("weekdays")
    start = str(schedule.get("start") or "").strip()
    end = str(schedule.get("end") or "").strip()
    if not isinstance(weekdays, list) or not weekdays or not start or not end:
        return ToolResult(
            success=False,
            error="Internal availability schedule is not configured",
        )
    if day.weekday() not in {int(x) for x in weekdays}:
        return ToolResult(
            success=True,
            data={"available": False, "date": raw_date, "available_slots": []},
        )
    try:
        start_dt = _parse_hhmm(start)
        end_dt = _parse_hhmm(end)
        slot_minutes = max(5, min(int(schedule.get("slot_minutes") or 30), 720))
        capacity = max(1, min(int(schedule.get("capacity") or 1), 100))
    except Exception:
        return ToolResult(
            success=False,
            error="Internal availability schedule is invalid",
        )
    if end_dt <= start_dt:
        return ToolResult(
            success=False,
            error="Availability end time must be after start time",
        )

    rows = (
        db.query(ActionRequest)
        .filter(
            ActionRequest.company_id == context["company_id"],
            ActionRequest.agent_id == context["agent_id"],
            ActionRequest.action_type == action_type,
            ActionRequest.status.in_(ACTIVE_SLOT_STATUSES),
        )
        .order_by(ActionRequest.id.desc())
        .limit(3000)
        .all()
    )
    occupied: dict[str, int] = {}
    requested_resource = (
        str(details.get(resource_field) or "").strip() if resource_field else ""
    )
    for row in rows:
        row_details = row.details or {}
        if str(row_details.get(date_field) or "").strip() != raw_date:
            continue
        if (
            resource_field
            and requested_resource
            and str(row_details.get(resource_field) or "").strip()
            != requested_resource
        ):
            continue
        row_time = str(row_details.get(time_field) or "").strip()
        if row_time:
            occupied[row_time] = occupied.get(row_time, 0) + 1

    slots = []
    cursor = start_dt
    while cursor + timedelta(minutes=slot_minutes) <= end_dt:
        value = cursor.strftime("%H:%M")
        if occupied.get(value, 0) < capacity:
            slots.append(value)
        cursor += timedelta(minutes=slot_minutes)

    requested_time = str(details.get(time_field) or "").strip()
    if requested_time:
        return ToolResult(
            success=True,
            data={
                "available": requested_time in slots,
                "date": raw_date,
                "time": requested_time,
                "available_slots": slots[:40],
            },
        )
    return ToolResult(
        success=True,
        data={
            "available": bool(slots),
            "date": raw_date,
            "available_slots": slots[:40],
        },
    )


def _instagram_publish_call(
    *,
    config: dict,
    payload: dict,
    operation: str,
    idempotency_key: str | None = None,
) -> ToolResult:
    if operation != "execute":
        return ToolResult(
            success=False,
            error="Instagram publishing currently supports execute only",
        )

    instagram_user_id = str(config.get("instagram_user_id") or "").strip()
    access_token = str(config.get("access_token") or "").strip()
    if not instagram_user_id or not access_token:
        return ToolResult(
            success=False,
            error="Instagram publishing connection is incomplete",
        )

    details = payload.get("details") if isinstance(payload, dict) else {}
    if not isinstance(details, dict):
        details = {}
    image_url = str(
        details.get("image_url")
        or details.get("media_url")
        or ""
    ).strip()
    caption = str(
        details.get("caption")
        or details.get("ai_response")
        or details.get("text")
        or ""
    ).strip()
    if not image_url:
        return ToolResult(
            success=False,
            error="Instagram publishing requires image_url or media_url",
        )
    stable_key = str(idempotency_key or "").strip()
    if not stable_key:
        return ToolResult(
            success=False,
            error="Instagram publishing requires a stable idempotency key",
        )
    claim_key = f"instagram_publish:{stable_key}"
    if not execution_claims.claim(claim_key, ttl_seconds=86400):
        return ToolResult(
            success=False,
            data={"reconciliation_required": True},
            error="Instagram publish is already claimed; manual reconciliation is required",
        )

    try:
        validate_public_http_url(image_url)
        container = safe_http_request(
            url=f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{instagram_user_id}/media",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            form_data={
                "image_url": image_url,
                "caption": caption[:2200],
            },
            timeout=20,
            max_response_bytes=128_000,
        )
    except Exception as exc:
        execution_claims.release(claim_key)
        return ToolResult(success=False, error=str(exc))

    container_status = int(container.get("status_code") or 0)
    if not 200 <= container_status < 300:
        execution_claims.release(claim_key)
        return ToolResult(
            success=False,
            data={"container_http": container},
            error=f"Instagram media container returned HTTP {container_status}",
        )
    try:
        container_body = json.loads(container.get("response") or "{}")
    except ValueError:
        execution_claims.release(claim_key)
        return ToolResult(
            success=False,
            data={"container_http": container},
            error="Instagram media container returned invalid JSON",
        )
    creation_id = str(container_body.get("id") or "").strip()
    if not creation_id:
        execution_claims.release(claim_key)
        return ToolResult(
            success=False,
            data={"container_http": container},
            error="Instagram media container did not return a creation id",
        )

    try:
        published = safe_http_request(
            url=f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{instagram_user_id}/media_publish",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}"},
            form_data={"creation_id": creation_id},
            timeout=20,
            max_response_bytes=128_000,
        )
    except Exception as exc:
        return ToolResult(success=False, error=str(exc))

    publish_status = int(published.get("status_code") or 0)
    if not 200 <= publish_status < 300:
        return ToolResult(
            success=False,
            data={
                "creation_id": creation_id,
                "publish_http": published,
            },
            error=f"Instagram publish returned HTTP {publish_status}",
        )
    try:
        publish_body = json.loads(published.get("response") or "{}")
    except ValueError:
        publish_body = {}

    return ToolResult(
        success=True,
        data={
            "provider": "instagram",
            "creation_id": creation_id,
            "media_id": publish_body.get("id"),
        },
        error=None,
    )


def _email_send_call(
    *,
    config: dict,
    payload: dict,
    operation: str,
    idempotency_key: str | None = None,
) -> ToolResult:
    if operation != "execute":
        return ToolResult(
            success=False,
            error="Email SMTP currently supports execute only",
        )
    stable_key = str(idempotency_key or "").strip()
    if not stable_key:
        return ToolResult(
            success=False,
            error="Email sending requires a stable idempotency key",
        )
    claim_key = f"email_send:{stable_key}"
    if not execution_claims.claim(claim_key, ttl_seconds=86400):
        return ToolResult(
            success=False,
            data={"reconciliation_required": True},
            error="Email send is already claimed; reconciliation is required before retrying",
        )

    details = payload.get("details") if isinstance(payload, dict) else {}
    if not isinstance(details, dict):
        details = {}
    recipient = str(
        details.get("to")
        or details.get("to_email")
        or details.get("recipient")
        or details.get("email")
        or ""
    ).strip()
    subject = str(
        details.get("subject")
        or details.get("title")
        or "Message from Xvond"
    ).strip()
    body = str(
        details.get("body")
        or details.get("text")
        or details.get("message")
        or details.get("ai_response")
        or ""
    )
    reply_to = str(details.get("reply_to") or "").strip() or None

    try:
        result = send_smtp_email(
            config=config,
            to_address=recipient,
            subject=subject,
            body=body,
            reply_to=reply_to,
        )
    except EmailConnectorError as exc:
        execution_claims.release(claim_key)
        return ToolResult(success=False, error=str(exc))

    return ToolResult(
        success=True,
        data={
            **result,
            "idempotency_key": stable_key,
        },
    )


def _email_read_call(
    *,
    config: dict,
    payload: dict,
    operation: str,
) -> ToolResult:
    if operation != "execute":
        return ToolResult(
            success=False,
            error="Email IMAP currently supports execute only",
        )
    details = payload.get("details") if isinstance(payload, dict) else {}
    if not isinstance(details, dict):
        details = {}
    raw_unread = details.get("unread_only", True)
    if isinstance(raw_unread, str):
        unread_only = raw_unread.strip().lower() not in {"0", "false", "no", "all"}
    else:
        unread_only = bool(raw_unread)
    try:
        limit = int(details.get("limit") or 10)
    except (TypeError, ValueError):
        return ToolResult(success=False, error="Email read limit must be a number")
    try:
        result = read_imap_messages(
            config=config,
            unread_only=unread_only,
            limit=limit,
        )
    except EmailReadConnectorError as exc:
        return ToolResult(success=False, error=str(exc))
    return ToolResult(success=True, data=result)


def _ensure_fresh_oauth_access_token(
    db,
    integration: CompanyIntegration,
    config: dict,
) -> tuple[dict, str | None]:
    oauth_config = config.get("_xvond_oauth")
    flow = (
        str(oauth_config.get("flow") or "")
        if isinstance(oauth_config, dict)
        else ""
    )
    if flow not in {"authorization_code", "client_credentials"}:
        return config, None

    try:
        if not oauth_access_token_needs_refresh(oauth_config):
            return config, None
    except ValueError as exc:
        return config, str(exc)

    if (
        flow == "authorization_code"
        and not str(oauth_config.get("refresh_token") or "").strip()
    ):
        return config, "OAuth access token has expired; reconnect this account"

    claim_key = f"oauth_refresh:{integration.company_id}:{integration.id}"
    if not execution_claims.claim(claim_key, ttl_seconds=300):
        try:
            db.expire(integration, ["config"])
            db.refresh(integration)
            latest = reveal_config(integration.config) or {}
            latest_oauth = latest.get("_xvond_oauth")
            if (
                isinstance(latest_oauth, dict)
                and not oauth_access_token_needs_refresh(latest_oauth)
            ):
                return latest, None
        except Exception:
            pass
        return config, "OAuth token refresh is already in progress; retry the request"

    try:
        try:
            if flow == "authorization_code":
                token = refresh_oauth_access_token(oauth_config)
            else:
                token = oauth_client_credentials_token(
                    {
                        "flow": "client_credentials",
                        "token_url": oauth_config.get("token_url"),
                        "scopes": oauth_config.get("scopes") or [],
                    },
                    client_id=str(oauth_config.get("client_id") or ""),
                    client_secret=str(oauth_config.get("client_secret") or ""),
                )
        except ValueError as exc:
            return config, f"OAuth token refresh failed: {exc}"

        updated = dict(config)
        updated_oauth = dict(oauth_config)
        if flow == "authorization_code":
            updated_oauth["refresh_token"] = token["refresh_token"]
        updated_oauth["expires_in"] = token.get("expires_in")
        if token.get("scope") is not None:
            updated_oauth["scope"] = token.get("scope")
        updated_oauth.pop("expires_at", None)
        updated_oauth.update(oauth_token_timing(token.get("expires_in")))
        updated["api_key"] = token["access_token"]
        updated["_xvond_oauth"] = updated_oauth
        integration.config = updated
        db.commit()
        db.refresh(integration)
        return reveal_config(integration.config) or {}, None
    finally:
        execution_claims.release(claim_key)

MAX_STRUCTURED_INTEGRATION_RESPONSE_CHARS = 250_000


def _integration_response_value(result: dict):
    """Expose bounded JSON responses as structured data for later graph nodes."""
    raw = result.get("response") if isinstance(result, dict) else None
    if not isinstance(raw, str):
        return raw
    if len(raw) > MAX_STRUCTURED_INTEGRATION_RESPONSE_CHARS:
        raw = raw[:MAX_STRUCTURED_INTEGRATION_RESPONSE_CHARS]
    if bool(result.get("truncated")):
        return raw
    text = raw.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return raw


def _integration_call(
    db,
    context: dict,
    action_type: str,
    action: dict,
    payload: dict,
    operation: str,
    *,
    idempotency_key: str | None = None,
) -> ToolResult:
    destination = action.get("destination") or {}
    integration_id = destination.get("integration_id")
    if not integration_id:
        return ToolResult(
            success=False,
            error="No integration is selected for this action",
        )
    integration = (
        db.query(CompanyIntegration)
        .filter(
            CompanyIntegration.id == int(integration_id),
            CompanyIntegration.company_id == context["company_id"],
            CompanyIntegration.enabled.is_(True),
        )
        .first()
    )
    if integration is None:
        return ToolResult(
            success=False,
            error="Configured integration is unavailable",
        )
    config = reveal_config(integration.config) or {}
    config, oauth_error = _ensure_fresh_oauth_access_token(db, integration, config)
    if oauth_error:
        return ToolResult(success=False, error=oauth_error)
    if (
        destination.get("validation_required") is True
        and not integration_validation_ready(config)
    ):
        return ToolResult(
            success=False,
            error="Configured integration must be validated again before use",
        )
    operations = destination.get("operations") or {}
    if not operations and isinstance(config.get("operations"), dict):
        operations = config.get("operations") or {}
    if (
        operation == "execute"
        and isinstance(operations, dict)
        and not isinstance(operations.get("execute"), dict)
    ):
        default_operation = str(destination.get("default_operation") or "").strip()
        if default_operation and isinstance(operations.get(default_operation), dict):
            operation = default_operation
    op_config = operations.get(operation) if isinstance(operations, dict) else None
    effective_op_config = op_config if isinstance(op_config, dict) else destination
    method = str(effective_op_config.get("method") or "POST").upper()
    headers = {
        "Content-Type": "application/json",
        **(effective_op_config.get("headers") or {}),
    }
    if idempotency_key:
        headers.setdefault("Idempotency-Key", idempotency_key)
        headers.setdefault("X-Xvond-Idempotency-Key", idempotency_key)
    integration_type = integration.integration_type
    request_payload = payload.get("details") if isinstance(payload, dict) else payload

    if integration_type == "instagram_publish":
        return _instagram_publish_call(
            config=config,
            payload=payload,
            operation=operation,
            idempotency_key=idempotency_key,
        )

    if integration_type == "email_smtp":
        return _email_send_call(
            config=config,
            payload=payload,
            operation=operation,
            idempotency_key=idempotency_key,
        )

    if integration_type == "email_imap":
        return _email_read_call(
            config=config,
            payload=payload,
            operation=operation,
        )

    if integration_type == "calendar":
        try:
            result = execute_google_calendar_operation(
                config=config,
                payload=payload,
                operation=operation,
                idempotency_key=idempotency_key,
            )
        except CalendarConnectorError as exc:
            return ToolResult(
                success=False,
                error=str(exc),
                data={
                    "reconciliation_required": (
                        "outcome is unknown" in str(exc).lower()
                    ),
                },
            )
        return ToolResult(success=True, data=result)

    if integration_type == "webhook":
        url = str(config.get("url") or "").strip()
        secret = config.get("secret")
        if secret:
            headers.setdefault("X-Xvond-Webhook-Secret", str(secret))
    elif integration_type in {"custom_api", "pos", "crm", "erp"}:
        if isinstance(operations, dict) and operations and not isinstance(op_config, dict):
            return ToolResult(
                success=False,
                error=f"API operation '{operation}' is not configured for this connection",
            )
        op_config = op_config if isinstance(op_config, dict) else destination
        base_url = str(config.get("base_url") or "").strip().rstrip("/")
        endpoint = str(op_config.get("endpoint") or "").strip()
        if not base_url or not endpoint:
            return ToolResult(
                success=False,
                error="This integration action needs a configured API endpoint",
            )
        if endpoint.lower().startswith(("http://", "https://")) or endpoint.startswith(
            "//"
        ):
            return ToolResult(
                success=False,
                error="Integration endpoint must be a relative path",
            )
        path_params = op_config.get("path_params")
        if not isinstance(path_params, list):
            path_params = re.findall(r"{([A-Za-z_][A-Za-z0-9_]{0,63})}", endpoint)
        clean_payload = dict(request_payload) if isinstance(request_payload, dict) else {}
        for raw_name in path_params[:20]:
            name = str(raw_name or "").strip()
            placeholder = "{" + name + "}"
            if not name or placeholder not in endpoint:
                continue
            if name not in clean_payload or clean_payload[name] is None:
                return ToolResult(
                    success=False,
                    error=f"API operation '{operation}' requires path parameter '{name}'",
                )
            endpoint = endpoint.replace(
                placeholder,
                quote(str(clean_payload.pop(name)), safe=""),
            )
        if "{" in endpoint or "}" in endpoint:
            return ToolResult(
                success=False,
                error=f"API operation '{operation}' has unresolved path parameters",
            )
        request_payload = clean_payload
        url = base_url + "/" + endpoint.lstrip("/")
    else:
        return ToolResult(
            success=False,
            error=(
                f"Integration type '{integration_type}' does not yet have "
                "a real execution adapter"
            ),
        )

    input_mode = str(
        (op_config or {}).get("input_mode") or ("query" if method == "GET" else "json")
    ).strip().lower()
    if input_mode not in {"json", "query", "none"}:
        return ToolResult(success=False, error="Integration operation input mode is invalid")
    if input_mode == "json":
        source = request_payload if isinstance(request_payload, dict) else {}
        required_json_fields = [
            str(item).strip()
            for item in ((op_config or {}).get("required_json_fields") or [])
            if str(item or "").strip()
        ]
        raw_json_fields = (op_config or {}).get("json_fields")
        raw_json_fields = raw_json_fields if isinstance(raw_json_fields, list) else []
        declared_json_fields = [
            str(item.get("key") or "").strip()
            for item in raw_json_fields
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        ]
        allowed_json_fields = list(
            dict.fromkeys([*declared_json_fields, *required_json_fields])
        )
        if allowed_json_fields:
            source = {
                key: value
                for key, value in source.items()
                if key in allowed_json_fields
            }
            request_payload = source
        missing_json_fields = [
            key
            for key in required_json_fields
            if key not in source or source.get(key) in (None, "")
        ]
        if missing_json_fields:
            return ToolResult(
                success=False,
                error=(
                    f"API operation '{operation}' requires JSON field(s): "
                    + ", ".join(missing_json_fields)
                ),
                data={"missing_fields": missing_json_fields},
            )

    if input_mode == "query":
        query_items = []
        source = request_payload if isinstance(request_payload, dict) else {}
        raw_query_params = (op_config or {}).get("query_params")
        raw_query_params = raw_query_params if isinstance(raw_query_params, list) else []
        query_params = [
            str(item).strip()
            for item in raw_query_params
            if str(item or "").strip()
        ]
        required_query_params = [
            str(item).strip()
            for item in ((op_config or {}).get("required_query_params") or [])
            if str(item or "").strip()
        ]
        allowed_query_params = list(
            dict.fromkeys([*query_params, *required_query_params])
        )
        if allowed_query_params:
            source = {
                key: value
                for key, value in source.items()
                if key in allowed_query_params
            }
        missing_query_params = [
            key
            for key in required_query_params
            if key not in source or source.get(key) in (None, "")
        ]
        if missing_query_params:
            return ToolResult(
                success=False,
                error=(
                    f"API operation '{operation}' requires query parameter(s): "
                    + ", ".join(missing_query_params)
                ),
            )
        for key, value in source.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                query_items.append((str(key), str(value)))
            elif isinstance(value, list) and all(
                isinstance(item, (str, int, float, bool)) for item in value
            ):
                query_items.extend((str(key), str(item)) for item in value)
            else:
                return ToolResult(
                    success=False,
                    error=f"Query parameter '{key}' must be scalar or a scalar list",
                )
        if query_items:
            separator = "&" if "?" in url else "?"
            url = url + separator + urlencode(query_items)

    if integration_type in {"custom_api", "pos", "crm", "erp"}:
        try:
            url, headers = apply_http_api_auth(
                url=url,
                headers=headers,
                config=config,
            )
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

    try:
        validate_public_http_url(url)
        result = safe_http_request(
            url=url,
            method=method,
            headers=headers,
            json_data=request_payload if input_mode == "json" else None,
            timeout=float((op_config or {}).get("timeout") or 15),
            max_response_bytes=MAX_STRUCTURED_INTEGRATION_RESPONSE_CHARS,
        )
    except Exception as exc:
        return ToolResult(success=False, error=str(exc))
    status = int(result.get("status_code") or 0)
    success = 200 <= status < 300
    response_value = _integration_response_value(result)
    return ToolResult(
        success=success,
        data={
            "integration_id": integration.id,
            "integration": integration.name,
            "status_code": status,
            "response": response_value,
            "http": result,
            "idempotency_key": idempotency_key,
        },
        error=None if success else f"Integration returned HTTP {status}",
    )


def _handoff(
    db,
    context: dict,
    request: ActionRequest,
    action: dict,
    priority: str,
) -> dict:
    reason = (
        f"{request.action_type} request #{request.id}: {request.summary or ''}".strip()
    )
    handoff = HumanHandoff(
        company_id=context["company_id"],
        agent_id=context["agent_id"],
        conversation_id=context.get("conversation_id"),
        reason=reason,
        priority=priority,
        department=str(
            (action.get("destination") or {}).get("department")
            or "customer_service"
        ),
    )
    db.add(handoff)
    db.flush()
    session = None
    if context.get("conversation_id") is not None:
        session = (
            db.query(WhatsAppSession)
            .filter(
                WhatsAppSession.company_id == context["company_id"],
                WhatsAppSession.agent_id == context["agent_id"],
                WhatsAppSession.conversation_id == context["conversation_id"],
            )
            .first()
        )
        if session is not None:
            activate_human_handoff(session, reason=reason)
    return {"handoff_id": handoff.id, "ai_paused": session is not None}


def _customer_details(details: dict) -> dict:
    return {
        key: value
        for key, value in (details or {}).items()
        if not str(key).startswith("_xvond_")
    }


def _execution_state(request: ActionRequest) -> dict:
    details = request.details or {}
    value = details.get("_xvond_execution")
    return dict(value) if isinstance(value, dict) else {}


def _save_execution_state(
    request: ActionRequest,
    *,
    state: str,
    key: str,
    operation: str,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    details = dict(request.details or {})
    meta = {
        "state": state,
        "key": key,
        "operation": operation,
        "updated_at": datetime.utcnow().isoformat(),
    }
    if error:
        meta["error"] = str(error)[:1000]
    if result is not None:
        meta["result"] = result
    details["_xvond_execution"] = meta
    request.details = details


class ActionRequestTool(AgentTool):
    name = "action_request"
    description = (
        "Run configured real business actions for this employee. Actions may save "
        "inside Xvond, check internal availability, hand off to a human, or execute "
        "a configured external integration."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "check_availability",
                    "prepare",
                    "execute",
                    "cancel",
                    "status",
                ],
            },
            "action_type": {"type": "string"},
            "details": {"type": "object", "additionalProperties": True},
            "summary": {"type": "string"},
            "request_id": {"type": "integer"},
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
            },
        },
        "required": ["operation", "action_type"],
        "additionalProperties": False,
    }

    def execute(self, arguments, context):
        db = context["db"]
        config = context.get("config", {}) or {}
        operation = str(arguments.get("operation") or "").strip()
        action_type = str(arguments.get("action_type") or "").strip()
        action = _action(config, action_type)
        if action is None:
            return ToolResult(
                success=False,
                error="This business action is not configured or enabled",
            )
        details = arguments.get("details") or {}
        if not isinstance(details, dict):
            return ToolResult(
                success=False,
                error="Action details must be structured",
            )
        destination = action.get("destination") or {}
        destination_type = str(
            destination.get("type") or "unconfigured"
        ).strip()
        availability = action.get("availability") or {}
        availability_mode = str(availability.get("mode") or "none").strip()

        if operation == "check_availability":
            if availability_mode == "xvond_schedule":
                return _internal_slots(
                    db,
                    context,
                    action_type,
                    action,
                    details,
                )
            if availability_mode == "integration":
                payload = {
                    "operation": "check_availability",
                    "action_type": action_type,
                    "details": details,
                }
                return _integration_call(
                    db,
                    context,
                    action_type,
                    action,
                    payload,
                    "availability",
                )
            return ToolResult(
                success=False,
                error="This action has no availability source configured",
            )

        if operation == "prepare":
            missing = _missing(action, details)
            if missing:
                return ToolResult(
                    success=False,
                    error=(
                        "Missing required customer details: "
                        + ", ".join(missing)
                    ),
                    data={"missing_fields": missing},
                )
            if destination_type == "unconfigured":
                return ToolResult(
                    success=False,
                    error="This action has no real destination configured",
                )
            if availability_mode == "xvond_schedule":
                availability_result = _internal_slots(
                    db,
                    context,
                    action_type,
                    action,
                    details,
                )
                if not availability_result.success:
                    return availability_result
                requested_time = str(
                    details.get(_schedule_fields(action)[1]) or ""
                ).strip()
                if (
                    not (availability_result.data or {}).get("available")
                    or requested_time
                    not in (availability_result.data or {}).get(
                        "available_slots", []
                    )
                ):
                    return ToolResult(
                        success=False,
                        error="Requested time is not available",
                        data=availability_result.data,
                    )
            summary = _summary(action, details, arguments.get("summary"))
            existing = None
            if context.get("conversation_id") is not None:
                existing = (
                    db.query(ActionRequest)
                    .filter(
                        ActionRequest.company_id == context["company_id"],
                        ActionRequest.agent_id == context["agent_id"],
                        ActionRequest.conversation_id == context["conversation_id"],
                        ActionRequest.action_type == action_type,
                        ActionRequest.status == "awaiting_confirmation",
                    )
                    .order_by(ActionRequest.id.desc())
                    .first()
                )
            if existing:
                request = existing
                request.details = dict(details)
                request.summary = summary
            else:
                request = ActionRequest(
                    company_id=context["company_id"],
                    agent_id=context["agent_id"],
                    conversation_id=context.get("conversation_id"),
                    action_type=action_type,
                    details=dict(details),
                    summary=summary,
                    status="awaiting_confirmation",
                )
                db.add(request)
                db.flush()
            if not bool(action.get("confirmation_required", True)):
                return self._execute_request(
                    request,
                    action,
                    arguments,
                    context,
                )
            return ToolResult(
                success=True,
                data={
                    "action": "prepared",
                    "request_id": request.id,
                    "status": request.status,
                    "summary": request.summary,
                    "details": request.details,
                    "confirmation_required": True,
                },
            )

        request_id = arguments.get("request_id")
        if not request_id:
            return ToolResult(
                success=False,
                error="request_id is required for this operation",
            )
        request = (
            db.query(ActionRequest)
            .filter(
                ActionRequest.id == int(request_id),
                ActionRequest.company_id == context["company_id"],
                ActionRequest.agent_id == context["agent_id"],
                ActionRequest.action_type == action_type,
            )
            .first()
        )
        if request is None:
            return ToolResult(success=False, error="Business request not found")

        if operation == "status":
            return ToolResult(
                success=True,
                data={
                    "request_id": request.id,
                    "status": request.status,
                    "summary": request.summary,
                    "details": request.details,
                },
            )
        if operation == "cancel":
            return self._cancel_request(request, action, context)
        if operation == "execute":
            return self._execute_request(request, action, arguments, context)
        return ToolResult(success=False, error="Unsupported action operation")

    def _cancel_request(
        self,
        request: ActionRequest,
        action: dict,
        context: dict,
    ) -> ToolResult:
        db = context["db"]
        if request.status == "cancelled":
            return ToolResult(
                success=True,
                data={
                    "request_id": request.id,
                    "status": "cancelled",
                    "already_cancelled": True,
                },
            )

        destination = action.get("destination") or {}
        destination_type = str(destination.get("type") or "unconfigured")
        if destination_type != "integration" or request.status in {
            "awaiting_confirmation",
            "new",
        }:
            request.status = "cancelled"
            return ToolResult(
                success=True,
                data={"request_id": request.id, "status": request.status},
            )

        current = _execution_state(request)
        if current.get("operation") == "cancel" and current.get("state") == "confirmed":
            request.status = "cancelled"
            return ToolResult(
                success=True,
                data={
                    "request_id": request.id,
                    "status": request.status,
                    "already_cancelled": True,
                },
            )
        if current.get("state") in {"executing", "external_failed"}:
            return ToolResult(
                success=False,
                error=(
                    "External execution is unresolved. Reconcile this request "
                    "before attempting cancellation."
                ),
                data={"request_id": request.id, "execution": current},
            )

        operations = destination.get("operations") or {}
        cancel_cfg = (
            (operations.get("cancel") or {})
            if isinstance(operations, dict)
            else {}
        )
        if not cancel_cfg:
            return ToolResult(
                success=False,
                error="This external action has no cancellation operation configured",
            )

        idempotency_key = (
            f"xvond-action-{context['company_id']}-{request.id}-cancel-v1"
        )
        _save_execution_state(
            request,
            state="executing",
            key=idempotency_key,
            operation="cancel",
        )
        request.status = "cancelling"
        db.commit()
        db.refresh(request)

        result = _integration_call(
            db,
            context,
            request.action_type,
            action,
            {
                "operation": "cancel",
                "request_id": request.id,
                "details": _customer_details(request.details or {}),
            },
            "cancel",
            idempotency_key=idempotency_key,
        )
        if not result.success:
            _save_execution_state(
                request,
                state="external_failed",
                key=idempotency_key,
                operation="cancel",
                error=result.error,
                result=result.data,
            )
            request.status = "external_failed"
            db.commit()
            return result

        _save_execution_state(
            request,
            state="confirmed",
            key=idempotency_key,
            operation="cancel",
            result=result.data,
        )
        request.status = "cancelled"
        db.commit()
        return ToolResult(
            success=True,
            data={
                "request_id": request.id,
                "status": request.status,
                "integration_result": result.data,
            },
        )

    def _execute_request(
        self,
        request: ActionRequest,
        action: dict,
        arguments: dict,
        context: dict,
    ) -> ToolResult:
        db = context["db"]
        execution = _execution_state(request)
        if (
            request.status == "confirmed"
            and execution.get("operation") == "execute"
            and execution.get("state") == "confirmed"
        ):
            return ToolResult(
                success=True,
                data={
                    "action": "executed",
                    "request_id": request.id,
                    "status": request.status,
                    "already_executed": True,
                    "execution": execution,
                },
            )
        if request.status in {"executing", "external_failed", "cancelling"}:
            return ToolResult(
                success=False,
                error=(
                    "This external request has an unresolved execution state and "
                    "will not be retried automatically. Reconcile it first."
                ),
                data={"request_id": request.id, "execution": execution},
            )
        if request.status not in {"awaiting_confirmation", "new"}:
            return ToolResult(
                success=False,
                error=f"Request cannot be executed from status {request.status}",
            )
        if bool(action.get("confirmation_required", True)):
            latest = _latest_user_message(db, context.get("conversation_id"))
            if not _customer_confirmed(latest):
                return ToolResult(
                    success=False,
                    error="Customer confirmation is required before execution",
                )

        availability = action.get("availability") or {}
        if str(availability.get("mode") or "none") == "xvond_schedule":
            date_field, time_field, resource_field = _schedule_fields(action)
            lock_key = (
                f"{context['company_id']}:{context['agent_id']}:"
                f"{request.action_type}:{request.details.get(date_field)}:"
                f"{request.details.get(time_field)}:"
                f"{request.details.get(resource_field) if resource_field else ''}"
            )
            if (
                getattr(getattr(db, "bind", None), "dialect", None) is not None
                and db.bind.dialect.name == "postgresql"
            ):
                db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": lock_key},
                )
            availability_result = _internal_slots(
                db,
                context,
                request.action_type,
                action,
                request.details or {},
            )
            if not availability_result.success:
                return availability_result
            if not (availability_result.data or {}).get("available"):
                return ToolResult(
                    success=False,
                    error="Requested time is no longer available",
                    data=availability_result.data,
                )
            requested_time = str(
                (request.details or {}).get(time_field) or ""
            ).strip()
            if requested_time not in (availability_result.data or {}).get(
                "available_slots", []
            ):
                return ToolResult(
                    success=False,
                    error="Requested time is no longer available",
                    data=availability_result.data,
                )

        destination = action.get("destination") or {}
        destination_type = str(destination.get("type") or "unconfigured")
        if destination_type == "xvond_internal":
            request.status = (
                "confirmed"
                if str(availability.get("mode") or "none") != "none"
                else "new"
            )
            meta = dict(request.details or {})
            meta["_xvond_destination"] = {"type": "xvond_internal"}
            request.details = meta
            event_name = (
                "booking.created"
                if str(availability.get("mode") or "none") != "none"
                else f"{request.action_type}.created"
            )
            event = enqueue_automation_event(
                db,
                company_id=context["company_id"],
                event_name=event_name,
                event_id=f"action-request:{request.id}:{request.status}",
                source_type="action_request",
                source_id=request.id,
                payload={
                    "request_id": request.id,
                    "agent_id": context["agent_id"],
                    "action_type": request.action_type,
                    "status": request.status,
                    "summary": request.summary,
                    "details": _customer_details(request.details or {}),
                },
            )
            db.commit()
            return ToolResult(
                success=True,
                data={
                    "action": "executed",
                    "request_id": request.id,
                    "status": request.status,
                    "summary": request.summary,
                    "event": {
                        "outbox_id": event.id,
                        "event_name": event.event_name,
                        "event_id": event.event_id,
                        "status": event.status,
                    },
                },
            )
        if destination_type == "human_handoff":
            request.status = "pending_human"
            handoff_data = _handoff(
                db,
                context,
                request,
                action,
                str(arguments.get("priority") or "normal"),
            )
            return ToolResult(
                success=True,
                data={
                    "action": "handed_off",
                    "request_id": request.id,
                    "status": request.status,
                    **handoff_data,
                },
            )
        if destination_type == "integration":
            idempotency_key = (
                f"xvond-action-{context['company_id']}-{request.id}-execute-v1"
            )
            customer_details = _customer_details(request.details or {})
            _save_execution_state(
                request,
                state="executing",
                key=idempotency_key,
                operation="execute",
            )
            request.status = "executing"
            db.commit()
            db.refresh(request)

            payload = {
                "operation": "execute",
                "request_id": request.id,
                "action_type": request.action_type,
                "details": customer_details,
                "summary": request.summary,
            }
            result = _integration_call(
                db,
                context,
                request.action_type,
                action,
                payload,
                "execute",
                idempotency_key=idempotency_key,
            )
            if not result.success:
                _save_execution_state(
                    request,
                    state="external_failed",
                    key=idempotency_key,
                    operation="execute",
                    error=result.error,
                    result=result.data,
                )
                request.status = "external_failed"
                db.commit()
                return result

            _save_execution_state(
                request,
                state="confirmed",
                key=idempotency_key,
                operation="execute",
                result=result.data,
            )
            request.status = "confirmed"
            meta = dict(request.details or {})
            meta["_xvond_destination"] = {
                "type": "integration",
                "integration_id": destination.get("integration_id"),
            }
            request.details = meta
            db.commit()
            return ToolResult(
                success=True,
                data={
                    "action": "executed",
                    "request_id": request.id,
                    "status": request.status,
                    "integration_result": result.data,
                },
            )
        return ToolResult(
            success=False,
            error="This action has no real destination configured",
        )


action_request_tool = ActionRequestTool()
