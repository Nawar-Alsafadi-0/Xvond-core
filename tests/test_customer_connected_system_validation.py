from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.api import customer_management as api
from backend.app.modules.integrations.models import CompanyIntegration


def _integration(kind, config):
    return CompanyIntegration(
        id=11,
        company_id=7,
        integration_type=kind,
        name="Test system",
        config=config,
        enabled=True,
    )


def test_generic_api_validation_uses_read_only_get_and_auth(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        api,
        "validate_public_http_url",
        lambda url: url,
    )

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 200, "response": "{}", "truncated": False}

    monkeypatch.setattr(api, "safe_http_request", fake_request)

    result = api._validate_live_connection(
        _integration(
            "custom_api",
            {
                "base_url": "https://api.example.com",
                "api_key": "secret-key",
                "validation_endpoint": "/me",
            },
        )
    )

    assert result["validated"] is True
    assert result["mode"] == "live_read_only_request"
    assert captured["url"] == "https://api.example.com/me"
    assert captured["method"] == "GET"
    assert captured["headers"]["Authorization"] == "Bearer secret-key"


def test_validation_endpoint_must_be_relative(monkeypatch):
    monkeypatch.setattr(api, "validate_public_http_url", lambda url: url)

    with pytest.raises(HTTPException) as exc:
        api._validate_live_connection(
            _integration(
                "crm",
                {
                    "base_url": "https://crm.example.com",
                    "validation_endpoint": "https://evil.example.com/check",
                },
            )
        )

    assert exc.value.status_code == 400


def test_connection_validation_fails_closed_on_provider_error(monkeypatch):
    monkeypatch.setattr(api, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(
        api,
        "safe_http_request",
        lambda **kwargs: {"status_code": 401},
    )

    with pytest.raises(HTTPException) as exc:
        api._validate_live_connection(
            _integration(
                "pos",
                {
                    "base_url": "https://pos.example.com",
                    "validation_endpoint": "/account",
                },
            )
        )

    assert exc.value.status_code == 409
    assert "HTTP 401" in str(exc.value.detail)


def test_internal_validation_evidence_is_not_public():
    item = _integration(
        "custom_api",
        {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "api_key": "secret",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-18T12:00:00Z",
                "status_code": 200,
            },
        },
    )

    rendered = api._serialize_integration(item)

    assert rendered["validated"] is True
    assert rendered["validated_at"] == "2026-09-18T12:00:00Z"
    assert "api_key" not in rendered["config"]
    assert "_xvond_validation" not in rendered["config"]
    assert "api_key" in rendered["configured_secret_fields"]
