from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from backend.app.core.config.settings import settings
from backend.app.main import app
from backend.app.modules.integrations import google_calendar
from backend.app.modules.integrations import google_calendar_oauth as oauth
from backend.app.modules.integrations.catalog import validate_integration_config


PORTAL = Path("frontend/customer/app.js").read_text(encoding="utf-8")
BUILDER = Path("frontend/customer/employee-builder.js").read_text(encoding="utf-8")
CUSTOMER_AGENTS = Path("backend/app/api/customer_agents.py").read_text(encoding="utf-8")


def _configure_oauth(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(
        settings,
        "GOOGLE_CALENDAR_OAUTH_REDIRECT_URI",
        "https://api.xvond.com/customer/agents/manage/integrations/google-calendar/oauth/callback",
    )
    monkeypatch.setattr(settings, "CONFIG_ENCRYPTION_KEY", "test-calendar-oauth-state-key")


def test_oauth_state_is_encrypted_signed_and_expiring(monkeypatch):
    _configure_oauth(monkeypatch)
    state = oauth.issue_google_calendar_oauth_state(
        user_id=10,
        company_id=20,
        integration_id=None,
        name="Clinic calendar",
        config={
            "calendar_id": "primary",
            "timezone": "Asia/Muscat",
            "slot_minutes": 30,
        },
        now=1_000,
    )

    assert "Clinic calendar" not in state
    assert "Asia/Muscat" not in state
    payload = oauth.verify_google_calendar_oauth_state(state, now=1_100)
    assert payload["user_id"] == 10
    assert payload["company_id"] == 20
    assert payload["config"]["timezone"] == "Asia/Muscat"

    with pytest.raises(oauth.GoogleCalendarOAuthError):
        oauth.verify_google_calendar_oauth_state(state + "x", now=1_100)

    with pytest.raises(oauth.GoogleCalendarOAuthError):
        oauth.verify_google_calendar_oauth_state(state, now=2_000)


def test_authorization_url_requests_offline_calendar_scopes(monkeypatch):
    _configure_oauth(monkeypatch)
    state = oauth.issue_google_calendar_oauth_state(
        user_id=1,
        company_id=2,
        integration_id=3,
        name="Calendar",
        config={
            "calendar_id": "primary",
            "timezone": "Asia/Muscat",
            "slot_minutes": 30,
        },
        now=1_000,
    )

    url = oauth.build_google_calendar_authorization_url(state=state)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == oauth.GOOGLE_AUTHORIZATION_URL
    assert query["client_id"] == ["client-id"]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["state"] == [state]
    assert set(query["scope"][0].split()) == set(oauth.GOOGLE_CALENDAR_SCOPES)


def test_token_exchange_keeps_client_secret_out_of_url(monkeypatch):
    _configure_oauth(monkeypatch)
    captured = {}
    monkeypatch.setattr(oauth, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": '{"access_token":"access","refresh_token":"refresh","expires_in":3600}',
        }

    monkeypatch.setattr(oauth, "safe_http_request", fake_request)

    result = oauth.exchange_google_calendar_code(code="authorization-code")

    assert result["access_token"] == "access"
    assert result["refresh_token"] == "refresh"
    assert captured["url"] == oauth.GOOGLE_TOKEN_URL
    assert "client-secret" not in captured["url"]
    assert captured["form_data"]["client_secret"] == "client-secret"


def test_calendar_refresh_uses_platform_credentials_without_customer_client_secret(monkeypatch):
    _configure_oauth(monkeypatch)
    assert google_calendar._refresh_credentials(
        {"refresh_token": "refresh-token"}
    ) == ("refresh-token", "client-id", "client-secret")

    assert validate_integration_config(
        "calendar",
        {
            "provider": "google",
            "timezone": "Asia/Muscat",
            "refresh_token": "refresh-token",
        },
    ) is True


def test_calendar_oauth_routes_are_registered_under_canonical_customer_prefix():
    paths = {route.path for route in app.routes}
    assert "/customer/agents/manage/integrations/google-calendar/oauth/status" in paths
    assert "/customer/agents/manage/integrations/google-calendar/oauth/start" in paths
    assert "/customer/agents/manage/integrations/google-calendar/oauth/callback" in paths
    assert "/manage/integrations/google-calendar/oauth/start" not in paths


def test_connected_system_ui_uses_registered_customer_management_routes():
    assert '"/manage/integrations' not in PORTAL
    assert '"/manage/integrations' not in BUILDER
    assert "/customer/agents/manage/integrations" in PORTAL
    assert "/customer/agents/manage/integrations" in BUILDER
    assert "Connect Google Calendar" in PORTAL
    assert "Reconnect Google Calendar" in PORTAL
    assert "router.include_router(customer_google_calendar_router)" in CUSTOMER_AGENTS
