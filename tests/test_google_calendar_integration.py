import json

import pytest

from backend.app.modules.integrations import google_calendar as calendar
from backend.app.modules.tools import action_request as action_runtime


def _config():
    return {
        "provider": "google",
        "calendar_id": "primary",
        "timezone": "Asia/Muscat",
        "slot_minutes": 30,
        "access_token": "secret-token",
        "_xvond_validation": {
            "validated": True,
            "validated_at": "2026-09-19T00:00:00Z",
        },
    }


def test_google_calendar_connection_validation_is_read_only(monkeypatch):
    captured = {}
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": json.dumps({"id": "primary", "summary": "Primary"}),
        }

    monkeypatch.setattr(calendar, "safe_http_request", fake_request)

    result = calendar.validate_google_calendar_connection(_config())

    assert result["validated"] is True
    assert result["mode"] == "google_calendar_live_read_only_request"
    assert result["timezone"] == "Asia/Muscat"
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/calendars/primary")
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in captured["url"]


def test_google_calendar_availability_uses_freebusy(monkeypatch):
    captured = {}
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": json.dumps(
                {"calendars": {"primary": {"busy": []}}}
            ),
        }

    monkeypatch.setattr(calendar, "safe_http_request", fake_request)

    result = calendar.google_calendar_availability(
        _config(),
        {"date": "2026-09-20", "time": "14:30"},
    )

    assert result["available"] is True
    assert result["timezone"] == "Asia/Muscat"
    assert captured["url"].endswith("/freeBusy")
    assert captured["method"] == "POST"
    assert captured["json_data"]["items"] == [{"id": "primary"}]
    assert captured["json_data"]["timeZone"] == "Asia/Muscat"


def test_google_calendar_create_is_provider_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        calls.append(kwargs)
        if kwargs["url"].endswith("/freeBusy"):
            return {
                "status_code": 200,
                "response": json.dumps(
                    {"calendars": {"primary": {"busy": []}}}
                ),
            }
        if kwargs["method"] == "POST" and kwargs["url"].endswith("/events"):
            event_id = kwargs["json_data"]["id"]
            return {
                "status_code": 200,
                "response": json.dumps(
                    {"id": event_id, "htmlLink": "https://calendar.google.com/event"}
                ),
            }
        raise AssertionError(kwargs)

    monkeypatch.setattr(calendar, "safe_http_request", fake_request)

    payload = {
        "request_id": 77,
        "details": {
            "customer_name": "Nawar",
            "phone": "+96800000000",
            "service": "Consultation",
            "date": "2026-09-20",
            "time": "14:30",
            "notes": "First visit",
        },
    }
    result = calendar.google_calendar_create(
        _config(),
        payload,
        idempotency_key="xvond-action-1-77-execute-v1",
    )

    assert result["event_id"] == calendar._event_id(77)
    assert result["already_exists"] is False
    event_call = calls[-1]
    assert event_call["json_data"]["id"] == calendar._event_id(77)
    assert event_call["json_data"]["start"]["timeZone"] == "Asia/Muscat"
    assert event_call["json_data"]["summary"] == "Consultation — Nawar"


def test_google_calendar_duplicate_create_reconciles_existing_event(monkeypatch):
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        if kwargs["url"].endswith("/freeBusy"):
            return {
                "status_code": 200,
                "response": json.dumps(
                    {"calendars": {"primary": {"busy": []}}}
                ),
            }
        if kwargs["method"] == "POST" and kwargs["url"].endswith("/events"):
            return {"status_code": 409, "response": "{}"}
        if kwargs["method"] == "GET" and "/events/" in kwargs["url"]:
            return {
                "status_code": 200,
                "response": json.dumps(
                    {
                        "id": calendar._event_id(77),
                        "htmlLink": "https://calendar.google.com/existing",
                    }
                ),
            }
        raise AssertionError(kwargs)

    monkeypatch.setattr(calendar, "safe_http_request", fake_request)

    result = calendar.google_calendar_create(
        _config(),
        {
            "request_id": 77,
            "details": {
                "customer_name": "Nawar",
                "service": "Consultation",
                "date": "2026-09-20",
                "time": "14:30",
            },
        },
        idempotency_key="stable-key",
    )

    assert result["already_exists"] is True
    assert result["event_id"] == calendar._event_id(77)


def test_google_calendar_create_rechecks_busy_slot(monkeypatch):
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(
        calendar,
        "safe_http_request",
        lambda **kwargs: {
            "status_code": 200,
            "response": json.dumps(
                {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": "2026-09-20T14:30:00+04:00",
                                    "end": "2026-09-20T15:00:00+04:00",
                                }
                            ]
                        }
                    }
                }
            ),
        },
    )

    with pytest.raises(calendar.CalendarConnectorError) as exc:
        calendar.google_calendar_create(
            _config(),
            {
                "request_id": 77,
                "details": {
                    "customer_name": "Nawar",
                    "service": "Consultation",
                    "date": "2026-09-20",
                    "time": "14:30",
                },
            },
            idempotency_key="stable-key",
        )
    assert "no longer available" in str(exc.value)


def test_google_calendar_cancel_is_idempotent_when_event_absent(monkeypatch):
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(
        calendar,
        "safe_http_request",
        lambda **kwargs: {"status_code": 404, "response": "{}"},
    )

    result = calendar.google_calendar_cancel(
        _config(),
        {"request_id": 77},
    )

    assert result["cancelled"] is True
    assert result["already_absent"] is True
    assert result["event_id"] == calendar._event_id(77)


def test_action_runtime_dispatches_calendar_adapter(monkeypatch):
    captured = {}
    integration = type(
        "Integration",
        (),
        {
            "id": 11,
            "company_id": 7,
            "integration_type": "calendar",
            "name": "Calendar",
            "enabled": True,
            "config": _config(),
        },
    )()

    class Query:
        def filter(self, *args):
            return self

        def first(self):
            return integration

    class Database:
        def query(self, *args):
            return Query()

    monkeypatch.setattr(
        action_runtime,
        "execute_google_calendar_operation",
        lambda **kwargs: captured.update(kwargs)
        or {"provider": "google_calendar", "available": True},
    )

    result = action_runtime._integration_call(
        Database(),
        {"company_id": 7},
        "booking",
        {
            "destination": {
                "type": "integration",
                "integration_id": 11,
                "validation_required": True,
                "operations": {
                    "availability": {"adapter": "google_calendar"},
                },
            }
        },
        {
            "operation": "check_availability",
            "details": {"date": "2026-09-20", "time": "14:30"},
        },
        "availability",
    )

    assert result.success is True
    assert captured["operation"] == "availability"
    assert captured["config"]["calendar_id"] == "primary"


def test_google_calendar_refreshes_expired_access_token_once(monkeypatch):
    calls = []
    config = {
        **_config(),
        "access_token": "expired-token",
        "refresh_token": "refresh-token",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }
    monkeypatch.setattr(calendar, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        calls.append(kwargs)
        if kwargs["url"] == "https://oauth2.googleapis.com/token":
            assert kwargs["form_data"]["refresh_token"] == "refresh-token"
            return {
                "status_code": 200,
                "response": json.dumps({"access_token": "fresh-token"}),
            }
        authorization = kwargs["headers"]["Authorization"]
        if authorization == "Bearer expired-token":
            return {"status_code": 401, "response": "{}"}
        assert authorization == "Bearer fresh-token"
        return {
            "status_code": 200,
            "response": json.dumps({"id": "primary"}),
        }

    monkeypatch.setattr(calendar, "safe_http_request", fake_request)

    result = calendar.validate_google_calendar_connection(config)

    assert result["validated"] is True
    assert any(call["url"] == "https://oauth2.googleapis.com/token" for call in calls)
    assert calls[-1]["headers"]["Authorization"] == "Bearer fresh-token"


def test_google_calendar_slot_lock_blocks_concurrent_create(monkeypatch):
    class Claims:
        def claim(self, key, *, ttl_seconds):
            assert key.startswith("google_calendar_slot:")
            assert ttl_seconds == 300
            return False

        def release(self, key):
            raise AssertionError("Unacquired slot lock must not be released")

    monkeypatch.setattr(calendar, "execution_claims", Claims())

    with pytest.raises(calendar.CalendarConnectorError) as exc:
        calendar.google_calendar_create(
            _config(),
            {
                "request_id": 88,
                "details": {
                    "customer_name": "Nawar",
                    "service": "Consultation",
                    "date": "2026-09-20",
                    "time": "14:30",
                },
            },
            idempotency_key="stable-key",
        )

    assert "being booked" in str(exc.value)
