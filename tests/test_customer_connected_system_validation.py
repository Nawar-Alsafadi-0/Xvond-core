from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.api import customer_management as api
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.tools import action_request as action_runtime
from backend.app.modules.tools.models import AgentToolAssignment


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


def test_instagram_publish_validation_uses_bearer_token_without_exposing_it_in_url(monkeypatch):
    captured = {}

    monkeypatch.setattr(api, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 200, "response": '{"id":"178414000","username":"brand"}'}

    monkeypatch.setattr(api, "safe_http_request", fake_request)

    result = api._validate_live_connection(
        _integration(
            "instagram_publish",
            {
                "instagram_user_id": "178414000",
                "access_token": "secret-token",
            },
        )
    )

    assert result["validated"] is True
    assert result["mode"] == "instagram_live_read_only_request"
    assert captured["url"] == "https://graph.facebook.com/v26.0/178414000?fields=id,username"
    assert "secret-token" not in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer secret-token"


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


def test_customer_validated_action_fails_closed_after_connection_changes(monkeypatch):
    integration = _integration(
        "custom_api",
        {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "api_key": "rotated-secret",
        },
    )

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
        "safe_http_request",
        lambda **kwargs: pytest.fail("Unvalidated integrations must not execute"),
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
                    "execute": {"method": "POST", "endpoint": "/bookings"}
                },
            }
        },
        {"customer_name": "Test"},
        "execute",
    )

    assert result.success is False
    assert result.error == "Configured integration must be validated again before use"


@pytest.fixture
def connected_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine, autoflush=False)
    monkeypatch.setattr(api, "SessionLocal", factory)

    with factory() as db:
        db.add(
            Company(
                id=7,
                name="Self Service",
                active=False,
                lifecycle_status="onboarding",
                onboarding_source="self_service",
            )
        )
        db.flush()
        db.add(
            AIAgent(
                id=21,
                company_id=7,
                name="Booking employee",
                system_prompt="Book appointments",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.flush()
        db.add(
            AgentConfig(
                agent_id=21,
                agent_type="employee",
                settings={
                    "employee_builder": {
                        "compiled_at": "2026-09-18T12:00:00Z",
                        "last_tested_at": "2026-09-18T12:05:00Z",
                        "last_tested_compiled_at": "2026-09-18T12:00:00Z",
                        "compiled_spec": {
                            "requirements": [
                                {
                                    "key": "booking",
                                    "integration_id": 11,
                                    "validation_required": True,
                                }
                            ]
                        },
                    }
                },
            )
        )
        db.add(
            CompanyIntegration(
                id=11,
                company_id=7,
                integration_type="custom_api",
                name="Booking API",
                config={
                    "base_url": "https://api.example.com",
                    "validation_endpoint": "/me",
                    "api_key": "original-secret",
                    "_xvond_validation": {
                        "validated": True,
                        "validated_at": "2026-09-18T12:00:00Z",
                    },
                },
                enabled=True,
            )
        )
        db.add(
            AgentToolAssignment(
                agent_id=21,
                tool_name="action_request",
                config={
                    "actions": {
                        "booking": {
                            "destination": {
                                "type": "integration",
                                "integration_id": 11,
                                "validation_required": True,
                            }
                        }
                    }
                },
                enabled=True,
            )
        )
        db.commit()

    yield factory
    engine.dispose()


def test_compiled_employee_contract_keeps_connection_bound_without_assignment(
    connected_database,
):
    factory = connected_database
    with factory() as db:
        db.query(AgentToolAssignment).delete()
        db.commit()
        assert api._integration_bound_agent_ids(
            db,
            company_id=7,
            integration_id=11,
        ) == [21]


def test_config_change_invalidates_preview_and_is_blocked_while_live(
    connected_database,
):
    factory = connected_database
    user = SimpleNamespace(id=None, company_id=7)

    result = api.customer_integration_update(
        11,
        api.CustomerIntegrationUpdate(config={"api_key": "rotated-secret"}),
        user,
    )

    assert result["validated"] is False
    with factory() as db:
        builder = (
            db.query(AgentConfig)
            .filter_by(agent_id=21)
            .one()
            .settings["employee_builder"]
        )
        assert "last_tested_at" not in builder
        assert "last_tested_compiled_at" not in builder
        db.get(AIAgent, 21).enabled = True
        db.commit()

    with pytest.raises(HTTPException) as exc:
        api.customer_integration_update(
            11,
            api.CustomerIntegrationUpdate(config={"api_key": "newer-secret"}),
            user,
        )

    assert exc.value.status_code == 409
    assert "Stage a revision" in str(exc.value.detail)
