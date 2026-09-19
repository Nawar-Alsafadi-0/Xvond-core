from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

from backend.app.core.config.settings import settings
from cryptography.fernet import Fernet, InvalidToken

from backend.app.core.http_security import safe_http_request, validate_public_http_url


GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
    "https://www.googleapis.com/auth/calendar.calendars.readonly",
)
STATE_TTL_SECONDS = 600


class GoogleCalendarOAuthError(ValueError):
    pass


def google_calendar_oauth_ready() -> bool:
    return bool(
        str(settings.GOOGLE_CALENDAR_OAUTH_CLIENT_ID or "").strip()
        and str(settings.GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET or "").strip()
        and google_calendar_oauth_redirect_uri()
    )


def google_calendar_oauth_redirect_uri() -> str:
    configured = str(settings.GOOGLE_CALENDAR_OAUTH_REDIRECT_URI or "").strip()
    if configured:
        return configured
    base = str(settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    return (
        f"{base}/customer/agents/manage/integrations/google-calendar/oauth/callback"
        if base
        else ""
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _state_cipher() -> Fernet:
    source = str(settings.CONFIG_ENCRYPTION_KEY or settings.JWT_SECRET or "").strip()
    if not source:
        raise GoogleCalendarOAuthError("Google Calendar OAuth state encryption is not configured")
    key = base64.urlsafe_b64encode(
        hashlib.sha256(
            f"{source}:google-calendar-oauth-state:v1".encode("utf-8")
        ).digest()
    )
    return Fernet(key)


def issue_google_calendar_oauth_state(
    *,
    user_id: int,
    company_id: int,
    integration_id: int | None,
    name: str,
    config: dict,
    now: int | None = None,
) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = {
        "v": 1,
        "user_id": int(user_id),
        "company_id": int(company_id),
        "integration_id": int(integration_id) if integration_id is not None else None,
        "name": str(name or "").strip()[:200],
        "config": {
            "provider": "google",
            "calendar_id": str(config.get("calendar_id") or "primary").strip()[:1024],
            "timezone": str(config.get("timezone") or "").strip()[:100],
            "slot_minutes": int(config.get("slot_minutes") or 30),
        },
        "nonce": secrets.token_urlsafe(24),
        "iat": issued_at,
        "exp": issued_at + STATE_TTL_SECONDS,
    }
    encoded = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    state = _state_cipher().encrypt(encoded).decode("ascii")
    return state


def verify_google_calendar_oauth_state(
    state: str | None,
    *,
    now: int | None = None,
) -> dict:
    token = str(state or "").strip()
    if not token:
        raise GoogleCalendarOAuthError("Invalid Google Calendar OAuth state")
    try:
        payload = json.loads(
            _state_cipher().decrypt(token.encode("ascii")).decode("utf-8")
        )
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise GoogleCalendarOAuthError("Invalid Google Calendar OAuth state") from exc

    current = int(time.time() if now is None else now)
    if payload.get("v") != 1:
        raise GoogleCalendarOAuthError("Unsupported Google Calendar OAuth state")
    if int(payload.get("iat") or 0) > current + 60:
        raise GoogleCalendarOAuthError("Invalid Google Calendar OAuth state issue time")
    if int(payload.get("exp") or 0) <= current:
        raise GoogleCalendarOAuthError("Google Calendar OAuth state has expired")
    if not str(payload.get("nonce") or "").strip():
        raise GoogleCalendarOAuthError("Google Calendar OAuth state is incomplete")
    return payload


def build_google_calendar_authorization_url(*, state: str) -> str:
    if not google_calendar_oauth_ready():
        raise GoogleCalendarOAuthError("Google Calendar OAuth is not configured")
    params = {
        "client_id": settings.GOOGLE_CALENDAR_OAUTH_CLIENT_ID,
        "redirect_uri": google_calendar_oauth_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GOOGLE_CALENDAR_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return GOOGLE_AUTHORIZATION_URL + "?" + urlencode(params)


def exchange_google_calendar_code(*, code: str) -> dict:
    if not google_calendar_oauth_ready():
        raise GoogleCalendarOAuthError("Google Calendar OAuth is not configured")
    try:
        result = safe_http_request(
            url=validate_public_http_url(GOOGLE_TOKEN_URL),
            method="POST",
            headers={"Accept": "application/json"},
            form_data={
                "code": str(code or "").strip(),
                "client_id": settings.GOOGLE_CALENDAR_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET,
                "redirect_uri": google_calendar_oauth_redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=15,
            max_response_bytes=128_000,
        )
    except Exception as exc:
        raise GoogleCalendarOAuthError("Google OAuth token exchange failed") from exc

    status = int(result.get("status_code") or 0)
    if not 200 <= status < 300:
        raise GoogleCalendarOAuthError(
            f"Google OAuth token exchange returned HTTP {status}"
        )
    try:
        body = json.loads(result.get("response") or "{}")
    except ValueError as exc:
        raise GoogleCalendarOAuthError(
            "Google OAuth token exchange returned invalid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise GoogleCalendarOAuthError(
            "Google OAuth token exchange returned invalid JSON"
        )
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise GoogleCalendarOAuthError(
            "Google OAuth token exchange did not return an access token"
        )
    return {
        "access_token": access_token,
        "refresh_token": str(body.get("refresh_token") or "").strip() or None,
        "scope": str(body.get("scope") or "").strip(),
        "token_type": str(body.get("token_type") or "").strip(),
        "expires_in": body.get("expires_in"),
    }
