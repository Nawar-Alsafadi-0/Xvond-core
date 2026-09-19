from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.app.core.execution_claims import execution_claims
from backend.app.core.http_security import safe_http_request, validate_public_http_url


_GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
_MIN_SLOT_MINUTES = 5
_MAX_SLOT_MINUTES = 720


class CalendarConnectorError(ValueError):
    pass


def _provider(config: dict) -> str:
    provider = str(config.get("provider") or "google").strip().lower().replace("-", "_")
    if provider not in {"google", "google_calendar"}:
        raise CalendarConnectorError(
            "Calendar provider is not supported yet; use Google Calendar"
        )
    return "google"


def _calendar_id(config: dict) -> str:
    value = str(config.get("calendar_id") or "primary").strip()
    if len(value) > 1024 or any(ch in value for ch in "\r\n"):
        raise CalendarConnectorError("Calendar ID is invalid")
    return value


def _access_token(config: dict) -> str:
    return str(config.get("access_token") or "").strip()


def _refresh_credentials(config: dict) -> tuple[str, str, str] | None:
    refresh_token = str(config.get("refresh_token") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    client_secret = str(config.get("client_secret") or "").strip()
    if refresh_token and client_id and client_secret:
        return refresh_token, client_id, client_secret
    return None


def _refresh_access_token(config: dict) -> str:
    credentials = _refresh_credentials(config)
    if credentials is None:
        raise CalendarConnectorError(
            "Google Calendar needs an access token or OAuth refresh credentials"
        )
    refresh_token, client_id, client_secret = credentials
    try:
        result = safe_http_request(
            url=validate_public_http_url("https://oauth2.googleapis.com/token"),
            method="POST",
            headers={"Accept": "application/json"},
            form_data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
            max_response_bytes=128_000,
        )
    except Exception as exc:
        raise CalendarConnectorError("Google OAuth token refresh request failed") from exc
    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        raise CalendarConnectorError(f"Google OAuth token refresh returned HTTP {status}")
    try:
        body = json.loads(result.get("response") or "{}")
    except ValueError as exc:
        raise CalendarConnectorError("Google OAuth token refresh returned invalid JSON") from exc
    token = str(body.get("access_token") or "").strip() if isinstance(body, dict) else ""
    if not token:
        raise CalendarConnectorError("Google OAuth token refresh did not return an access token")
    return token


def _timezone(config: dict) -> ZoneInfo:
    name = str(config.get("timezone") or "").strip()
    if not name:
        raise CalendarConnectorError("Calendar timezone is required")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise CalendarConnectorError("Calendar timezone is invalid") from exc


def _slot_minutes(config: dict) -> int:
    try:
        value = int(config.get("slot_minutes") or 30)
    except (TypeError, ValueError) as exc:
        raise CalendarConnectorError("Calendar slot minutes must be a number") from exc
    if value < _MIN_SLOT_MINUTES or value > _MAX_SLOT_MINUTES:
        raise CalendarConnectorError(
            f"Calendar slot minutes must be between {_MIN_SLOT_MINUTES} and {_MAX_SLOT_MINUTES}"
        )
    return value


def _headers(access_token: str) -> dict:
    if not str(access_token or "").strip():
        raise CalendarConnectorError(
            "Google Calendar needs an access token or OAuth refresh credentials"
        )
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }


def _authorized_request(
    config: dict,
    *,
    url: str,
    method: str,
    json_data=None,
    timeout: float,
    max_response_bytes: int,
) -> dict:
    token = _access_token(config)
    if not token:
        token = _refresh_access_token(config)

    def send(current_token: str) -> dict:
        return safe_http_request(
            url=url,
            method=method,
            headers=_headers(current_token),
            json_data=json_data,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    result = send(token)
    if int(result.get("status_code") or 0) == 401 and _refresh_credentials(config):
        token = _refresh_access_token(config)
        result = send(token)
    return result


def _calendar_url(config: dict, suffix: str = "") -> str:
    calendar_id = quote(_calendar_id(config), safe="")
    base = f"{_GOOGLE_CALENDAR_API}/calendars/{calendar_id}"
    url = base + suffix
    return validate_public_http_url(url)


def _parse_google_response(result: dict, *, context: str) -> dict:
    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        raise CalendarConnectorError(f"{context} returned HTTP {status}")
    try:
        body = json.loads(result.get("response") or "{}")
    except ValueError as exc:
        raise CalendarConnectorError(f"{context} returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise CalendarConnectorError(f"{context} returned invalid JSON")
    return body


def validate_google_calendar_connection(
    config: dict,
    *,
    timeout: float = 10.0,
) -> dict:
    _provider(config)
    zone = _timezone(config)
    _slot_minutes(config)
    try:
        result = _authorized_request(
            config,
            url=_calendar_url(config),
            method="GET",
            timeout=timeout,
            max_response_bytes=128_000,
        )
        body = _parse_google_response(result, context="Google Calendar validation")
    except CalendarConnectorError:
        raise
    except Exception as exc:
        raise CalendarConnectorError("Google Calendar validation request failed") from exc

    return {
        "validated": True,
        "mode": "google_calendar_live_read_only_request",
        "status_code": int(result.get("status_code") or 0),
        "calendar_id": str(body.get("id") or _calendar_id(config))[:1024],
        "timezone": zone.key,
    }


def _local_interval(config: dict, details: dict) -> tuple[datetime, datetime]:
    raw_date = str(details.get("date") or "").strip()
    raw_time = str(details.get("time") or "").strip()
    if not raw_date:
        raise CalendarConnectorError("Booking date is required")
    if not raw_time:
        raise CalendarConnectorError(
            "Booking time is required to check external calendar availability"
        )
    try:
        day = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise CalendarConnectorError("Booking date must use YYYY-MM-DD") from exc

    try:
        clock = datetime.strptime(raw_time, "%H:%M").time()
    except ValueError as exc:
        raise CalendarConnectorError("Booking time must use HH:MM") from exc

    zone = _timezone(config)
    naive = datetime.combine(day, clock)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)

    # Reject local times that are ambiguous or do not exist during DST changes
    # rather than silently booking a different instant than the customer chose.
    first_valid = (
        first.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
    )
    second_valid = (
        second.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive
    )
    if not first_valid and not second_valid:
        raise CalendarConnectorError(
            "Booking time does not exist because of a daylight-saving transition"
        )
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        raise CalendarConnectorError(
            "Booking time is ambiguous because of a daylight-saving transition"
        )
    start = first if first_valid else second

    end = start + timedelta(minutes=_slot_minutes(config))
    return start, end


def _freebusy(
    config: dict,
    *,
    start: datetime,
    end: datetime,
    timeout: float = 15.0,
) -> dict:
    _provider(config)
    calendar_id = _calendar_id(config)
    url = validate_public_http_url(f"{_GOOGLE_CALENDAR_API}/freeBusy")
    try:
        result = _authorized_request(
            config,
            url=url,
            method="POST",
            json_data={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "timeZone": _timezone(config).key,
                "items": [{"id": calendar_id}],
            },
            timeout=timeout,
            max_response_bytes=256_000,
        )
        body = _parse_google_response(result, context="Google Calendar free/busy")
    except CalendarConnectorError:
        raise
    except Exception as exc:
        raise CalendarConnectorError("Google Calendar free/busy request failed") from exc

    calendars = body.get("calendars")
    calendar = calendars.get(calendar_id) if isinstance(calendars, dict) else None
    if not isinstance(calendar, dict):
        raise CalendarConnectorError(
            "Google Calendar free/busy response did not include the configured calendar"
        )
    errors = calendar.get("errors")
    if isinstance(errors, list) and errors:
        raise CalendarConnectorError("Google Calendar free/busy returned a calendar error")
    busy = calendar.get("busy")
    return {
        "busy": busy if isinstance(busy, list) else [],
        "http_status": int(result.get("status_code") or 0),
    }


def google_calendar_availability(config: dict, details: dict) -> dict:
    start, end = _local_interval(config, details)
    result = _freebusy(config, start=start, end=end)
    busy = result["busy"]
    return {
        "provider": "google_calendar",
        "available": not busy,
        "date": start.date().isoformat(),
        "time": start.strftime("%H:%M"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": _timezone(config).key,
        "busy": busy[:20],
    }


def _event_id(request_id: int) -> str:
    if int(request_id or 0) <= 0:
        raise CalendarConnectorError("Booking request ID is required")
    digest = hashlib.sha256(
        f"xvond-calendar-request:{int(request_id)}".encode("utf-8")
    ).hexdigest()
    # Google Calendar event ids accept lowercase base32hex characters. Hex is a
    # strict subset and a deterministic id makes retries provider-idempotent.
    return "e" + digest[:40]


def _event_body(config: dict, payload: dict) -> tuple[str, dict]:
    details = payload.get("details") if isinstance(payload, dict) else {}
    if not isinstance(details, dict):
        details = {}
    request_id = int(payload.get("request_id") or 0)
    start, end = _local_interval(config, details)
    customer_name = str(details.get("customer_name") or "").strip()
    service = str(details.get("service") or "").strip()
    phone = str(details.get("phone") or "").strip()
    notes = str(details.get("notes") or "").strip()

    if not customer_name:
        raise CalendarConnectorError("Customer name is required")
    if not service:
        raise CalendarConnectorError("Booking service is required")

    description_parts = []
    if phone:
        description_parts.append(f"Phone: {phone[:200]}")
    if notes:
        description_parts.append(f"Notes: {notes[:2000]}")
    description_parts.append(f"Xvond request: {request_id}")

    event_id = _event_id(request_id)
    body = {
        "id": event_id,
        "summary": f"{service} — {customer_name}"[:1024],
        "description": "\n".join(description_parts)[:8000],
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": _timezone(config).key,
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": _timezone(config).key,
        },
        "extendedProperties": {
            "private": {
                "xvond_request_id": str(request_id),
            }
        },
    }
    return event_id, body


def _event_lookup(config: dict, event_id: str) -> dict | None:
    try:
        result = _authorized_request(
            config,
            url=_calendar_url(config, f"/events/{quote(event_id, safe='')}"),
            method="GET",
            timeout=15,
            max_response_bytes=256_000,
        )
    except Exception as exc:
        raise CalendarConnectorError("Google Calendar event reconciliation failed") from exc

    status = int(result.get("status_code") or 0)
    if status == 404:
        return None
    return _parse_google_response(result, context="Google Calendar event lookup")


def _calendar_lock_scope(config: dict) -> str:
    client_id = str(config.get("client_id") or "").strip()
    refresh_token = str(config.get("refresh_token") or "").strip()
    access_token = _access_token(config)
    if refresh_token:
        identity = f"{client_id}:{refresh_token}"
    else:
        identity = access_token or client_id or "unconfigured"
    source = f"{_provider(config)}:{_calendar_id(config)}:{identity}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def google_calendar_create(
    config: dict,
    payload: dict,
    *,
    idempotency_key: str | None,
) -> dict:
    stable_key = str(idempotency_key or "").strip()
    if not stable_key:
        raise CalendarConnectorError("Calendar booking requires a stable idempotency key")

    details = payload.get("details") if isinstance(payload, dict) else {}
    if not isinstance(details, dict):
        details = {}
    start, end = _local_interval(config, details)
    calendar_scope = _calendar_lock_scope(config)
    slot_claim = (
        f"google_calendar_slot:{calendar_scope}:"
        f"{start.isoformat()}:{end.isoformat()}"
    )
    if not execution_claims.claim(slot_claim, ttl_seconds=300):
        raise CalendarConnectorError(
            "This calendar slot is being booked right now; check availability again"
        )

    try:
        availability = _freebusy(config, start=start, end=end)
        if availability["busy"]:
            raise CalendarConnectorError("Requested calendar time is no longer available")

        event_id, body = _event_body(config, payload)
        try:
            result = _authorized_request(
                config,
                url=_calendar_url(config, "/events"),
                method="POST",
                json_data=body,
                timeout=20,
                max_response_bytes=256_000,
            )
        except CalendarConnectorError:
            raise
        except Exception as exc:
            raise CalendarConnectorError(
                "Google Calendar create request outcome is unknown; reconcile before retrying"
            ) from exc
    finally:
        execution_claims.release(slot_claim)

    status = int(result.get("status_code") or 0)
    if status == 409:
        existing = _event_lookup(config, event_id)
        if existing is None:
            raise CalendarConnectorError(
                "Google Calendar reported a duplicate event but it could not be reconciled"
            )
        return {
            "provider": "google_calendar",
            "event_id": event_id,
            "html_link": existing.get("htmlLink"),
            "already_exists": True,
            "idempotency_key": stable_key,
        }

    body_response = _parse_google_response(result, context="Google Calendar create")
    return {
        "provider": "google_calendar",
        "event_id": str(body_response.get("id") or event_id),
        "html_link": body_response.get("htmlLink"),
        "already_exists": False,
        "idempotency_key": stable_key,
    }


def google_calendar_cancel(config: dict, payload: dict) -> dict:
    request_id = int(payload.get("request_id") or 0)
    event_id = _event_id(request_id)
    try:
        result = _authorized_request(
            config,
            url=_calendar_url(config, f"/events/{quote(event_id, safe='')}"),
            method="DELETE",
            timeout=20,
            max_response_bytes=64_000,
        )
    except Exception as exc:
        raise CalendarConnectorError(
            "Google Calendar cancellation outcome is unknown; reconcile before retrying"
        ) from exc

    status = int(result.get("status_code") or 0)
    if status == 404:
        return {
            "provider": "google_calendar",
            "event_id": event_id,
            "cancelled": True,
            "already_absent": True,
        }
    if not 200 <= status < 300:
        raise CalendarConnectorError(f"Google Calendar cancel returned HTTP {status}")
    return {
        "provider": "google_calendar",
        "event_id": event_id,
        "cancelled": True,
        "already_absent": False,
    }


def execute_google_calendar_operation(
    *,
    config: dict,
    payload: dict,
    operation: str,
    idempotency_key: str | None = None,
) -> dict:
    _provider(config)
    if operation == "availability":
        details = payload.get("details") if isinstance(payload, dict) else {}
        if not isinstance(details, dict):
            details = {}
        return google_calendar_availability(config, details)
    if operation == "execute":
        return google_calendar_create(
            config,
            payload,
            idempotency_key=idempotency_key,
        )
    if operation == "cancel":
        return google_calendar_cancel(config, payload)
    raise CalendarConnectorError(f"Unsupported Google Calendar operation: {operation}")
