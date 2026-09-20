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



def test_google_calendar_validation_uses_packaged_connector(monkeypatch):
    captured = {}

    def fake_validate(config, *, timeout):
        captured["config"] = config
        captured["timeout"] = timeout
        return {
            "validated": True,
            "mode": "google_calendar_live_read_only_request",
            "status_code": 200,
            "calendar_id": "primary",
            "timezone": "Asia/Muscat",
        }

    monkeypatch.setattr(api, "validate_google_calendar_connection", fake_validate)

    result = api._validate_live_connection(
        _integration(
            "calendar",
            {
                "provider": "google",
                "calendar_id": "primary",
                "timezone": "Asia/Muscat",
                "access_token": "secret-token",
            },
        )
    )

    assert result["validated"] is True
    assert result["mode"] == "google_calendar_live_read_only_request"
    assert captured["timeout"] == 10.0
    assert captured["config"]["access_token"] == "secret-token"


def test_google_calendar_secrets_are_not_exposed_in_customer_config():
    item = _integration(
        "calendar",
        {
            "provider": "google",
            "calendar_id": "primary",
            "timezone": "Asia/Muscat",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "client_id": "public-client-id",
            "client_secret": "client-secret",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-19T00:00:00Z",
            },
        },
    )

    rendered = api._serialize_integration(item)

    assert rendered["config"]["provider"] == "google"
    assert rendered["config"]["calendar_id"] == "primary"
    assert rendered["config"]["timezone"] == "Asia/Muscat"
    assert rendered["config"]["client_id"] == "public-client-id"
    assert rendered["execution_adapter"] == "google_calendar"
    assert rendered["requirement_keys"] == ["booking"]
    assert rendered["allow_generic_alternatives"] is True
    assert rendered["operation_endpoints"] is False
    assert "access_token" not in rendered["config"]
    assert "refresh_token" not in rendered["config"]
    assert "client_secret" not in rendered["config"]
    assert {"access_token", "refresh_token", "client_secret"} <= set(
        rendered["configured_secret_fields"]
    )

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
    detail = str(exc.value.detail).lower()
    assert "live employee" in detail
    assert "stage a revision" in detail


def test_expired_generic_oauth_refreshes_before_business_action(
    connected_database,
    monkeypatch,
):
    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "execute": {
                    "method": "POST",
                    "endpoint": "/bookings",
                    "input_mode": "json",
                }
            },
            "auth_type": "bearer",
            "api_key": "expired-access",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
            "_xvond_oauth": {
                "flow": "authorization_code",
                "token_url": "https://accounts.example.com/oauth/token",
                "client_id": "client-1",
                "client_secret": "secret-1",
                "refresh_token": "refresh-1",
                "expires_at": 1,
            },
        }
        db.commit()

    monkeypatch.setattr(
        action_runtime,
        "refresh_oauth_access_token",
        lambda oauth_config: {
            "access_token": "fresh-access",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
            "scope": None,
            "token_type": "Bearer",
        },
    )
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 200, "response": '{"id":"booking-1"}'}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "booking",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {
                        "execute": {
                            "method": "POST",
                            "endpoint": "/bookings",
                            "input_mode": "json",
                        }
                    },
                }
            },
            {"details": {"customer_name": "Test"}},
            "execute",
            idempotency_key="booking-refresh-1",
        )
        assert result.success is True
        assert captured["headers"]["Authorization"] == "Bearer fresh-access"
        refreshed = action_runtime.reveal_config(db.get(CompanyIntegration, 11).config)
        assert refreshed["api_key"] == "fresh-access"
        assert refreshed["_xvond_oauth"]["refresh_token"] == "refresh-2"
        assert refreshed["_xvond_oauth"]["expires_at"] > refreshed["_xvond_oauth"]["obtained_at"]


def test_failed_generic_oauth_refresh_blocks_business_side_effect(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "execute": {"method": "POST", "endpoint": "/bookings"}
            },
            "auth_type": "bearer",
            "api_key": "expired-access",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
            "_xvond_oauth": {
                "flow": "authorization_code",
                "token_url": "https://accounts.example.com/oauth/token",
                "client_id": "client-1",
                "client_secret": "secret-1",
                "refresh_token": "refresh-1",
                "expires_at": 1,
            },
        }
        db.commit()

    def fail_refresh(_oauth_config):
        raise ValueError("provider rejected refresh")

    monkeypatch.setattr(action_runtime, "refresh_oauth_access_token", fail_refresh)
    monkeypatch.setattr(
        action_runtime,
        "safe_http_request",
        lambda **kwargs: pytest.fail("Business API must not run after refresh failure"),
    )

    with factory() as db:
        result = action_runtime._integration_call(
            db,
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
            {"details": {"customer_name": "Test"}},
            "execute",
            idempotency_key="booking-refresh-fail-1",
        )

    assert result.success is False
    assert "OAuth token refresh failed" in str(result.error)


def test_expired_client_credentials_oauth_reacquires_token_before_action(
    connected_database,
    monkeypatch,
):
    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "execute": {"method": "POST", "endpoint": "/bookings"}
            },
            "auth_type": "bearer",
            "api_key": "expired-client-token",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
            "_xvond_oauth": {
                "flow": "client_credentials",
                "token_url": "https://accounts.example.com/oauth/token",
                "scopes": ["orders.write"],
                "client_id": "client-1",
                "client_secret": "secret-1",
                "expires_at": 1,
            },
        }
        db.commit()

    token_call = {}

    def fake_client_credentials(flow, *, client_id, client_secret):
        token_call.update({
            "flow": flow,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        return {
            "access_token": "fresh-client-token",
            "token_type": "Bearer",
            "expires_in": 1800,
            "scope": "orders.write",
        }

    monkeypatch.setattr(
        action_runtime,
        "oauth_client_credentials_token",
        fake_client_credentials,
    )
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 200, "response": '{"id":"booking-2"}'}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
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
            {"details": {"customer_name": "Test"}},
            "execute",
            idempotency_key="booking-client-refresh-1",
        )
        assert result.success is True
        assert captured["headers"]["Authorization"] == "Bearer fresh-client-token"
        refreshed = action_runtime.reveal_config(db.get(CompanyIntegration, 11).config)
        assert refreshed["api_key"] == "fresh-client-token"
        assert refreshed["_xvond_oauth"]["expires_at"] > refreshed["_xvond_oauth"]["obtained_at"]

    assert token_call["client_id"] == "client-1"
    assert token_call["client_secret"] == "secret-1"
    assert token_call["flow"]["scopes"] == ["orders.write"]

def test_generic_api_missing_required_json_field_fails_before_side_effect(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "execute": {
                    "method": "POST",
                    "endpoint": "/appointments",
                    "input_mode": "json",
                    "required_json_fields": ["customer_name", "service_id"],
                }
            },
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(
        action_runtime,
        "safe_http_request",
        lambda **kwargs: pytest.fail("Incomplete JSON body must not reach provider"),
    )

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "booking",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {
                        "execute": {
                            "method": "POST",
                            "endpoint": "/appointments",
                            "input_mode": "json",
                            "required_json_fields": ["customer_name", "service_id"],
                        }
                    },
                }
            },
            {"details": {"customer_name": "Test Customer"}},
            "execute",
            idempotency_key="required-json-1",
        )

    assert result.success is False
    assert result.data["missing_fields"] == ["service_id"]
    assert "requires JSON field(s): service_id" in str(result.error)


def test_generic_api_complete_required_json_body_executes(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "execute": {
                    "method": "POST",
                    "endpoint": "/appointments",
                    "input_mode": "json",
                    "required_json_fields": ["customer_name", "service_id"],
                }
            },
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 201, "response": '{"id":"appt-1"}'}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "booking",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {
                        "execute": {
                            "method": "POST",
                            "endpoint": "/appointments",
                            "input_mode": "json",
                            "required_json_fields": ["customer_name", "service_id"],
                        }
                    },
                }
            },
            {"details": {"customer_name": "Test Customer", "service_id": 42}},
            "execute",
            idempotency_key="required-json-2",
        )

    assert result.success is True
    assert captured["json_data"] == {
        "customer_name": "Test Customer",
        "service_id": 42,
    }

def test_direct_execute_uses_bound_default_api_operation(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "create_record": {
                    "method": "POST",
                    "endpoint": "/records",
                    "input_mode": "json",
                    "required_json_fields": ["customer_name"],
                }
            },
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 201, "response": '{"id":"rec-1"}'}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "specialist_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "default_operation": "create_record",
                    "operations": {
                        "create_record": {
                            "method": "POST",
                            "endpoint": "/records",
                            "input_mode": "json",
                            "required_json_fields": ["customer_name"],
                        }
                    },
                }
            },
            {"details": {"customer_name": "Test Customer"}},
            "execute",
            idempotency_key="default-op-1",
        )

    assert result.success is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.example.com/records"
    assert captured["json_data"] == {"customer_name": "Test Customer"}

def test_generic_api_json_request_sends_only_declared_contract_fields(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/accounts/{account_id}/appointments",
        "input_mode": "json",
        "path_params": ["account_id"],
        "required_json_fields": ["customer_name"],
        "json_fields": [
            {"key": "customer_name", "required": True, "type": "string"},
            {"key": "notes", "required": False, "type": "string"},
        ],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 201, "response": '{"id":"appt-2"}'}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "booking",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {"execute": operation},
                }
            },
            {
                "details": {
                    "account_id": "acct-7",
                    "customer_name": "Test Customer",
                    "notes": "Window seat",
                    "ai_response": "internal model output",
                    "secret_context": {"must": "not leave xvond"},
                }
            },
            "execute",
            idempotency_key="shape-json-1",
        )

    assert result.success is True
    assert captured["url"] == "https://api.example.com/accounts/acct-7/appointments"
    assert captured["json_data"] == {
        "customer_name": "Test Customer",
        "notes": "Window seat",
    }


def test_generic_api_query_request_sends_only_declared_contract_parameters(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "GET",
        "endpoint": "/records",
        "input_mode": "query",
        "query_params": ["status", "limit"],
        "required_query_params": ["status"],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"lookup": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 200, "response": '{"items":[]}'}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "vendor_search",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {"lookup": operation},
                }
            },
            {
                "details": {
                    "status": "open",
                    "limit": 20,
                    "ai_response": "internal model output",
                    "secret_context": "internal-only",
                }
            },
            "lookup",
            idempotency_key="shape-query-1",
        )

    assert result.success is True
    assert captured["url"] == "https://api.example.com/records?status=open&limit=20"
    assert "ai_response" not in captured["url"]
    assert "secret_context" not in captured["url"]
    assert captured["json_data"] is None

def test_connected_api_response_contract_parses_json_and_preserves_text():
    assert action_runtime._integration_response_value(
        {"response": '{"id":"abc","items":[1,2]}', "truncated": False}
    ) == {"id": "abc", "items": [1, 2]}
    assert action_runtime._integration_response_value(
        {"response": "provider accepted request", "truncated": False}
    ) == "provider accepted request"
    assert action_runtime._integration_response_value(
        {"response": '{"id":"partial"', "truncated": True}
    ) == '{"id":"partial"'


def test_connected_api_runtime_exposes_structured_provider_response(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {
                "execute": {
                    "method": "POST",
                    "endpoint": "/records",
                    "input_mode": "json",
                    "required_json_fields": ["customer_name"],
                }
            },
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 201,
            "response": '{"id":"rec-42","state":"created"}',
            "truncated": False,
        }

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {
                        "execute": {
                            "method": "POST",
                            "endpoint": "/records",
                            "input_mode": "json",
                            "required_json_fields": ["customer_name"],
                        }
                    },
                }
            },
            {"details": {"customer_name": "Test Customer"}},
            "execute",
            idempotency_key="response-contract-1",
        )

    assert result.success is True
    assert result.data["status_code"] == 201
    assert result.data["response"] == {"id": "rec-42", "state": "created"}
    assert captured["max_response_bytes"] == action_runtime.MAX_STRUCTURED_INTEGRATION_RESPONSE_CHARS

def test_generic_api_urlencoded_form_is_shaped_and_sent_as_form_data(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/session",
        "input_mode": "form",
        "required_form_fields": ["username"],
        "form_fields": [
            {"key": "username", "required": True, "type": "string"},
            {"key": "remember", "required": False, "type": "boolean"},
        ],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": '{"session_id":"s-1"}',
            "truncated": False,
        }

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "vendor_session",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {"execute": operation},
                }
            },
            {
                "details": {
                    "username": "nawar",
                    "remember": True,
                    "ai_response": "internal-only",
                }
            },
            "execute",
            idempotency_key="form-1",
        )

    assert result.success is True
    assert captured["json_data"] is None
    assert captured["form_data"] == {
        "username": "nawar",
        "remember": True,
    }
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_generic_api_urlencoded_form_missing_required_field_fails_before_http(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/session",
        "input_mode": "form",
        "required_form_fields": ["username"],
        "form_fields": [
            {"key": "username", "required": True, "type": "string"},
        ],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(
        action_runtime,
        "safe_http_request",
        lambda **kwargs: pytest.fail("Incomplete form must not reach provider"),
    )

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "vendor_session",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "validation_required": True,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"ai_response": "internal-only"}},
            "execute",
            idempotency_key="form-missing-1",
        )

    assert result.success is False
    assert result.data["missing_fields"] == ["username"]
    assert "requires form field(s): username" in str(result.error)

def test_generic_api_root_json_array_is_shaped_and_sent(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/records/batch",
        "input_mode": "json_array",
        "array_item_kind": "object",
        "array_max_items": 100,
        "required_array_item_fields": ["sku"],
        "array_item_fields": [
            {"key": "sku", "required": True, "type": "string"},
            {"key": "quantity", "required": False, "type": "integer"},
        ],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 201,
            "response": '{"created":2}',
            "truncated": False,
        }

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "batch_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {
                "details": {
                    "items": [
                        {"sku": "A", "quantity": 2, "extra": "drop"},
                        {"sku": "B", "quantity": 1},
                    ],
                    "unrelated": "drop",
                }
            },
            "execute",
            idempotency_key="json-array-1",
        )

    assert result.success is True
    assert captured["json_data"] == [
        {"sku": "A", "quantity": 2},
        {"sku": "B", "quantity": 1},
    ]
    assert captured["form_data"] is None
    assert captured["multipart_data"] is None


def test_generic_api_root_json_array_rejects_invalid_items(
    connected_database,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/records/batch",
        "input_mode": "json_array",
        "array_item_kind": "object",
        "array_max_items": 2,
        "required_array_item_fields": ["sku"],
        "array_item_fields": [
            {"key": "sku", "required": True, "type": "string"},
        ],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    with factory() as db:
        missing = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "batch_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"items": [{"quantity": 1}]}},
            "execute",
            idempotency_key="json-array-invalid-1",
        )
    assert missing.success is False
    assert missing.data["item_index"] == 0
    assert missing.data["missing_fields"] == ["sku"]

    with factory() as db:
        too_many = action_runtime._integration_call(
            db,
            {"company_id": 7},
            "batch_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"items": [{"sku": "A"}, {"sku": "B"}, {"sku": "C"}]}},
            "execute",
            idempotency_key="json-array-invalid-2",
        )
    assert too_many.success is False
    assert "at most 2" in too_many.error

def test_generic_api_declared_header_is_required_and_sent_without_entering_json(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/records",
        "input_mode": "json",
        "header_params": ["X-Workspace-ID", "X-Region"],
        "required_header_params": ["X-Workspace-ID"],
        "required_json_fields": ["name"],
        "json_fields": [
            {"key": "name", "required": True, "type": "string"},
        ],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {"status_code": 201, "response": '{"id":"r-1"}', "truncated": False}

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    with factory() as db:
        missing = action_runtime._integration_call(
            db,
            {"company_id": 7, "agent_id": 21},
            "vendor_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"name": "Test"}},
            "execute",
            idempotency_key="headers-1",
        )
    assert missing.success is False
    assert missing.data["missing_fields"] == ["X-Workspace-ID"]
    assert captured == {}

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7, "agent_id": 21},
            "vendor_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {
                "details": {
                    "X-Workspace-ID": "workspace-7",
                    "X-Region": "me",
                    "name": "Test",
                }
            },
            "execute",
            idempotency_key="headers-2",
        )

    assert result.success is True
    assert captured["headers"]["X-Workspace-ID"] == "workspace-7"
    assert captured["headers"]["X-Region"] == "me"
    assert captured["json_data"] == {"name": "Test"}


def test_generic_api_tampered_unsafe_header_contract_fails_before_http(
    connected_database,
    monkeypatch,
):
    factory = connected_database
    operation = {
        "method": "POST",
        "endpoint": "/records",
        "input_mode": "json",
        "header_params": ["Authorization"],
        "required_header_params": ["Authorization"],
    }
    with factory() as db:
        integration = db.get(CompanyIntegration, 11)
        integration.config = {
            "base_url": "https://api.example.com",
            "validation_endpoint": "/me",
            "operations": {"execute": operation},
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
        }
        db.commit()

    monkeypatch.setattr(
        action_runtime,
        "safe_http_request",
        lambda **kwargs: pytest.fail("Unsafe dynamic header must fail before HTTP"),
    )

    with factory() as db:
        result = action_runtime._integration_call(
            db,
            {"company_id": 7, "agent_id": 21},
            "vendor_records",
            {
                "destination": {
                    "type": "integration",
                    "integration_id": 11,
                    "operations": {"execute": operation},
                }
            },
            {"details": {"Authorization": "blocked"}},
            "execute",
            idempotency_key="headers-unsafe-1",
        )

    assert result.success is False
    assert "unsafe header parameter" in str(result.error)
