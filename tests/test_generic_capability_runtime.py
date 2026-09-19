from types import SimpleNamespace

import pytest

from backend.app.modules.tools import generic_capability_runtime as runtime


class _Response:
    content = b'{"price": 9.5}'

    def raise_for_status(self):
        return None

    def json(self):
        return {"price": 9.5}


def test_generic_runtime_fetch_extract_compare_notify(monkeypatch):
    monkeypatch.setattr(
        runtime.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(runtime.httpx, "get", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(
        runtime,
        "_notification_event",
        lambda *args, **kwargs: (SimpleNamespace(id=77), False),
    )

    action_config = {
        "destination": {
            "type": "xvond_internal",
            "adapter": "generic_capability",
            "allowed_hosts": ["prices.example.com"],
            "execution_plan": [
                {"id": "fetch", "op": "http_get_json", "url_field": "url"},
                {"id": "price", "op": "extract", "source": "fetch", "path": "price"},
                {
                    "id": "matched",
                    "op": "compare",
                    "source": "price",
                    "operator": "lte",
                    "value_field": "target_price",
                },
                {
                    "id": "notify",
                    "op": "notify",
                    "when": "matched",
                    "title": "Price alert",
                    "message": "Target price reached.",
                },
            ],
        }
    }

    result = runtime.execute_generic_capability(
        object(),
        company_id=1,
        agent_id=2,
        action_type="price_monitor",
        action_config=action_config,
        details={"url": "https://prices.example.com/item/1", "target_price": 10},
        idempotency_key="same-request",
    )

    assert result["completed"] is True
    assert result["steps"]["fetch"]["fetched"] is True
    assert result["steps"]["price"]["extracted"] is True
    assert result["steps"]["matched"]["matched"] is True
    assert result["steps"]["notify"]["notification_event_id"] == 77


def test_generic_runtime_skips_notification_when_condition_is_false(monkeypatch):
    monkeypatch.setattr(
        runtime.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(runtime.httpx, "get", lambda *args, **kwargs: _Response())

    called = {"notify": False}

    def fail_if_called(*args, **kwargs):
        called["notify"] = True
        raise AssertionError("notification should have been skipped")

    monkeypatch.setattr(runtime, "_notification_event", fail_if_called)

    config = {
        "destination": {
            "allowed_hosts": ["prices.example.com"],
            "execution_plan": [
                {"id": "fetch", "op": "http_get_json", "url_field": "url"},
                {"id": "price", "op": "extract", "source": "fetch", "path": "price"},
                {"id": "matched", "op": "compare", "source": "price", "operator": "lt", "value": 5},
                {"id": "notify", "op": "notify", "when": "matched"},
            ],
        }
    }

    result = runtime.execute_generic_capability(
        object(),
        company_id=1,
        agent_id=2,
        action_type="price_monitor",
        action_config=config,
        details={"url": "https://prices.example.com/item/1"},
        idempotency_key="request-2",
    )

    assert called["notify"] is False
    assert result["steps"]["matched"]["matched"] is False
    assert result["steps"]["notify"]["skipped"] is True


def test_generic_runtime_requires_approved_https_host(monkeypatch):
    config = {
        "destination": {
            "allowed_hosts": ["approved.example.com"],
            "execution_plan": [{"id": "fetch", "op": "http_get_json", "url_field": "url"}],
        }
    }

    with pytest.raises(runtime.GenericCapabilityRuntimeError, match="not approved"):
        runtime.execute_generic_capability(
            object(),
            company_id=1,
            agent_id=2,
            action_type="monitor",
            action_config=config,
            details={"url": "https://evil.example.com/data"},
            idempotency_key="request-3",
        )


def test_generic_runtime_rejects_private_resolved_address(monkeypatch):
    monkeypatch.setattr(
        runtime.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    config = {
        "destination": {
            "allowed_hosts": ["prices.example.com"],
            "execution_plan": [{"id": "fetch", "op": "http_get_json", "url_field": "url"}],
        }
    }

    with pytest.raises(runtime.GenericCapabilityRuntimeError, match="non-public"):
        runtime.execute_generic_capability(
            object(),
            company_id=1,
            agent_id=2,
            action_type="monitor",
            action_config=config,
            details={"url": "https://prices.example.com/data"},
            idempotency_key="request-4",
        )


def test_generic_runtime_readiness_requires_plan_and_http_host():
    assert runtime.generic_capability_readiness({"destination": {}})["ready"] is False
    assert runtime.generic_capability_readiness(
        {"destination": {"execution_plan": [{"id": "n", "op": "notify"}]}}
    )["ready"] is True
    result = runtime.generic_capability_readiness(
        {"destination": {"execution_plan": [{"id": "f", "op": "http_get_json"}]}}
    )
    assert result == {"ready": False, "reason": "approved_https_host_required"}

def test_generic_runtime_readiness_rejects_unknown_operations():
    result = runtime.generic_capability_readiness(
        {
            "destination": {
                "execution_plan": [
                    {"id": "run", "op": "shell", "command": "echo unsafe"}
                ]
            }
        }
    )
    assert result == {"ready": False, "reason": "unsupported_runtime_operation"}


def test_generic_runtime_readiness_rejects_forward_or_missing_sources():
    result = runtime.generic_capability_readiness(
        {
            "destination": {
                "execution_plan": [
                    {"id": "value", "op": "extract", "source": "fetch", "path": "price"},
                    {"id": "fetch", "op": "http_get_json", "url_field": "url"},
                ],
                "allowed_hosts": ["prices.example.com"],
            }
        }
    )
    assert result == {"ready": False, "reason": "runtime_source_unavailable"}


def test_generic_runtime_readiness_rejects_duplicate_step_ids():
    result = runtime.generic_capability_readiness(
        {
            "destination": {
                "execution_plan": [
                    {"id": "notify", "op": "notify"},
                    {"id": "notify", "op": "notify"},
                ]
            }
        }
    )
    assert result == {"ready": False, "reason": "invalid_runtime_step_id"}

