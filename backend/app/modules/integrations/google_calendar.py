from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import quote, urlencode

from backend.app.core.config.settings import settings
from backend.app.core.http_security import safe_http_request


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)


class GoogleCalendarError(ValueError):
    pass


def google_calendar_redirect_uri() -> str:
    configured = str(settings.GOOGLE_CALENDAR_REDIRECT_URI or "").strip()
    if configured:
        if not configured.startswith("https://") and settings.is_production:
            raise GoogleCalendarError(
                "Google Calendar OAuth redirect URI must use HTTPS in production"
            )
        return configured
    base = str(settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise GoogleCalendarError(
            "PUBLIC_BASE_URL or GOOGLE_CALENDAR_REDIRECT_URI is required"
        )
    return base + "/manage/integrations/google-calendar/oauth/callback"


def google_calendar_oauth_ready() -> bool:
    return bool(
        str(settings.GOOGLE_OAUTH_CLIENT_ID or "").strip()
        and str(settings.GOOGLE_OAUTH_CLIENT_SECRET or "").strip()
        and (
            str(settings.GOOGLE_CALENDAR_REDIRECT_URI or "").strip()
            or str(settings.PUBLIC_BASE_URL or "").strip()
        )
    )


def build_google_calendar_authorization_url(*, state: str) -> str:
    if not google_calendar_oauth_ready():
        raise GoogleCalendarError(
            "Google Calendar OAuth is not configured on Xvond"
        )
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": google_calendar_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": str(state or ""),
    }
    return GOOGLE_AUTH_URL + "?" + urlencode(params)


def _json_response(result: dict, *, label: str) -> tuple[int, dict]:
    status = int(result.get("status_code") or 0)
    raw = str(result.get("response") or "")
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise GoogleCalendarError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GoogleCalendarError(f"{label} returned an invalid response")
    return status, payload


def _token_request(form_data: dict) -> dict:
    try:
        result = safe_http_request(
            url=GOOGLE_TOKEN_URL,
            method="POST",
            headers={"Accept": "application/json"},
            form_data=form_data,
            timeout=15,
            max_response_bytes=128_000,
        )
    except Exception as exc:
        raise GoogleCalendarError("Google OAuth token request failed") from exc
    status, payload = _json_response(result, label="Google OAuth")
    if not 200 <= status < 300:
        raise GoogleCalendarError(
            str(payload.get("error_description") or payload.get("error") or "Google OAuth rejected the token request")
        )
    return payload


def _token_expiry(expires_in) -> str:
    try:
        seconds = max(60, int(expires_in or 3600))
    except (TypeError, ValueError):
        seconds = 3600
    return (
        datetime.now(UTC) + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


def exchange_google_calendar_code(code: str) -> dict:
    clean_code = str(code or "").strip()
    if not clean_code:
        raise GoogleCalendarError("Google authorization code is required")
    if not google_calendar_oauth_ready():
        raise GoogleCalendarError(
            "Google Calendar OAuth is not configured on Xvond"
        )
    payload = _token_request(
        {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "code": clean_code,
            "grant_type": "authorization_code",
            "redirect_uri": google_calendar_redirect_uri(),
        }
    )
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not access_token:
        raise GoogleCalendarError("Google did not return an access token")
    if not refresh_token:
        raise GoogleCalendarError(
            "Google did not return offline access. Reconnect and grant consent again."
        )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_at": _token_expiry(payload.get("expires_in")),
        "oauth_scope": str(payload.get("scope") or " ".join(GOOGLE_CALENDAR_SCOPES)),
        "token_type": str(payload.get("token_type") or "Bearer"),
    }


def _parse_expiry(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def refresh_google_calendar_token(config: dict) -> dict:
    refresh_token = str(config.get("refresh_token") or "").strip()
    if not refresh_token:
        raise GoogleCalendarError(
            "Google Calendar refresh token is missing; reconnect the calendar"
        )
    payload = _token_request(
        {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    )
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise GoogleCalendarError("Google did not return a refreshed access token")
    return {
        "access_token": access_token,
        "access_token_expires_at": _token_expiry(payload.get("expires_in")),
        "oauth_scope": str(payload.get("scope") or config.get("oauth_scope") or ""),
        "token_type": str(payload.get("token_type") or "Bearer"),
    }


def ensure_google_calendar_access_token(config: dict) -> tuple[str, dict]:
    token = str(config.get("access_token") or "").strip()
    expiry = _parse_expiry(config.get("access_token_expires_at"))
    now = datetime.now(UTC)
    if token and expiry is not None and expiry > now + timedelta(seconds=90):
        return token, {}
    updates = refresh_google_calendar_token(config)
    return str(updates["access_token"]), updates


def _calendar_request(
    *,
    config: dict,
    method: str,
    url: str,
    json_data=None,
    max_response_bytes: int = 256_000,
) -> tuple[dict, dict]:
    token, updates = ensure_google_calendar_access_token(config)

    def request(current_token: str) -> dict:
        return safe_http_request(
            url=url,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {current_token}",
            },
            json_data=json_data,
            timeout=15,
            max_response_bytes=max_response_bytes,
        )

    try:
        result = request(token)
    except Exception as exc:
        raise GoogleCalendarError("Google Calendar request failed") from exc

    if int(result.get("status_code") or 0) == 401 and config.get("refresh_token"):
        refreshed = refresh_google_calendar_token({**config, **updates})
        updates.update(refreshed)
        token = str(refreshed["access_token"])
        try:
            result = request(token)
        except Exception as exc:
            raise GoogleCalendarError("Google Calendar retry failed") from exc

    return result, updates


def google_calendar_freebusy(
    *,
    config: dict,
    time_min: str,
    time_max: str,
) -> tuple[dict, dict]:
    calendar_id = str(config.get("calendar_id") or "primary").strip() or "primary"
    result, updates = _calendar_request(
        config=config,
        method="POST",
        url=GOOGLE_CALENDAR_API + "/freeBusy",
        json_data={
            "timeMin": str(time_min),
            "timeMax": str(time_max),
            "items": [{"id": calendar_id}],
        },
    )
    status, payload = _json_response(result, label="Google Calendar FreeBusy")
    if not 200 <= status < 300:
        raise GoogleCalendarError(
            str((payload.get("error") or {}).get("message") or f"Google Calendar FreeBusy returned HTTP {status}")
        )
    calendars = payload.get("calendars") or {}
    calendar = calendars.get(calendar_id) if isinstance(calendars, dict) else None
    if not isinstance(calendar, dict):
        raise GoogleCalendarError("Google Calendar did not return the requested calendar")
    if calendar.get("errors"):
        raise GoogleCalendarError("Google Calendar reported an availability error")
    busy = calendar.get("busy") or []
    return {
        "calendar_id": calendar_id,
        "busy": busy if isinstance(busy, list) else [],
    }, updates


def stable_google_event_id(idempotency_key: str) -> str:
    value = str(idempotency_key or "").strip()
    if not value:
        raise GoogleCalendarError("Stable idempotency key is required")
    # Google event IDs accept base32hex characters. A SHA-256 hex digest uses
    # only 0-9 and a-f, which is a valid subset.
    return "a" + sha256(value.encode("utf-8")).hexdigest()


def google_calendar_create_event(
    *,
    config: dict,
    idempotency_key: str,
    summary: str,
    start_rfc3339: str,
    end_rfc3339: str,
    description: str = "",
    attendees: list[str] | None = None,
) -> tuple[dict, dict]:
    calendar_id = str(config.get("calendar_id") or "primary").strip() or "primary"
    event_id = stable_google_event_id(idempotency_key)
    attendee_rows = [
        {"email": str(item).strip()}
        for item in (attendees or [])
        if str(item).strip()
    ]
    body = {
        "id": event_id,
        "summary": str(summary or "Xvond booking")[:1000],
        "description": str(description or "")[:8000],
        "start": {"dateTime": str(start_rfc3339)},
        "end": {"dateTime": str(end_rfc3339)},
    }
    if attendee_rows:
        body["attendees"] = attendee_rows

    base_url = (
        GOOGLE_CALENDAR_API
        + "/calendars/"
        + quote(calendar_id, safe="")
        + "/events"
    )
    query = "?sendUpdates=all" if attendee_rows else "?sendUpdates=none"
    result, updates = _calendar_request(
        config=config,
        method="POST",
        url=base_url + query,
        json_data=body,
    )
    status, payload = _json_response(result, label="Google Calendar create event")

    if status == 409:
        existing, refresh_updates = _calendar_request(
            config={**config, **updates},
            method="GET",
            url=base_url + "/" + quote(event_id, safe=""),
        )
        updates.update(refresh_updates)
        existing_status, existing_payload = _json_response(
            existing,
            label="Google Calendar existing event",
        )
        if 200 <= existing_status < 300:
            payload = existing_payload
            status = existing_status

    if not 200 <= status < 300:
        raise GoogleCalendarError(
            str((payload.get("error") or {}).get("message") or f"Google Calendar create event returned HTTP {status}")
        )

    return {
        "provider": "google_calendar",
        "provider_event_id": str(payload.get("id") or event_id),
        "calendar_id": calendar_id,
        "html_link": str(payload.get("htmlLink") or ""),
        "status": str(payload.get("status") or "confirmed"),
    }, updates


def google_calendar_delete_event(
    *,
    config: dict,
    event_id: str,
) -> tuple[dict, dict]:
    clean_event_id = str(event_id or "").strip()
    if not clean_event_id:
        raise GoogleCalendarError("Google Calendar event ID is required for cancellation")
    calendar_id = str(config.get("calendar_id") or "primary").strip() or "primary"
    result, updates = _calendar_request(
        config=config,
        method="DELETE",
        url=(
            GOOGLE_CALENDAR_API
            + "/calendars/"
            + quote(calendar_id, safe="")
            + "/events/"
            + quote(clean_event_id, safe="")
            + "?sendUpdates=all"
        ),
    )
    status = int(result.get("status_code") or 0)
    if status in {404, 410}:
        return {
            "provider": "google_calendar",
            "provider_event_id": clean_event_id,
            "already_deleted": True,
        }, updates
    if not 200 <= status < 300:
        _, payload = _json_response(result, label="Google Calendar delete event")
        raise GoogleCalendarError(
            str((payload.get("error") or {}).get("message") or f"Google Calendar delete event returned HTTP {status}")
        )
    return {
        "provider": "google_calendar",
        "provider_event_id": clean_event_id,
        "deleted": True,
    }, updates


def validate_google_calendar_connection(config: dict) -> tuple[dict, dict]:
    now = datetime.now(UTC)
    result, updates = google_calendar_freebusy(
        config=config,
        time_min=now.isoformat().replace("+00:00", "Z"),
        time_max=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )
    return {
        "validated": True,
        "mode": "google_calendar_freebusy",
        "calendar_id": result["calendar_id"],
    }, updates
