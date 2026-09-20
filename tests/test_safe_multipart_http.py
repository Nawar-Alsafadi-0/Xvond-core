from types import SimpleNamespace

from backend.app.core import http_security
from backend.app.modules.tools import action_request as action_runtime


def test_safe_http_request_encodes_scalar_multipart_with_httpx_boundary(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def iter_bytes(self):
            yield b"{}"

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["request"] = kwargs
            return Response()

    monkeypatch.setattr(http_security, "validate_public_http_url", lambda url: url)
    monkeypatch.setattr(http_security.httpx, "Client", Client)

    result = http_security.safe_http_request(
        "https://api.example.com/submit",
        method="POST",
        multipart_data={"title": "Hello", "count": 3},
    )

    assert result["status_code"] == 200
    request = captured["request"]
    assert request["json"] is None
    assert request["data"] is None
    assert request["files"] == {
        "title": (None, "Hello"),
        "count": (None, "3"),
    }


def test_generic_api_scalar_multipart_is_shaped_before_http(monkeypatch):
    captured = {}
    integration = SimpleNamespace(
        id=11,
        company_id=7,
        enabled=True,
        integration_type="custom_api",
        name="Vendor API",
        config={
            "base_url": "https://api.example.com",
            "auth_type": "none",
            "_xvond_validation": {
                "validated": True,
                "validated_at": "2026-09-20T00:00:00Z",
            },
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

    monkeypatch.setattr(action_runtime, "validate_public_http_url", lambda url: url)

    def fake_request(**kwargs):
        captured.update(kwargs)
        return {
            "status_code": 200,
            "response": '{"accepted":true}',
            "truncated": False,
        }

    monkeypatch.setattr(action_runtime, "safe_http_request", fake_request)

    operation = {
        "method": "POST",
        "endpoint": "/submit",
        "input_mode": "multipart",
        "required_form_fields": ["title"],
        "form_fields": [
            {"key": "title", "required": True, "type": "string"},
            {"key": "count", "required": False, "type": "integer"},
        ],
    }
    result = action_runtime._integration_call(
        Database(),
        {"company_id": 7},
        "vendor_submit",
        {
            "destination": {
                "type": "integration",
                "integration_id": 11,
                "operations": {"execute": operation},
            }
        },
        {"details": {"title": "Hello", "count": 3, "extra": "ignored"}},
        "execute",
        idempotency_key="multipart-scalar-1",
    )

    assert result.success is True
    assert captured["multipart_data"] == {"title": "Hello", "count": 3}
    assert captured["form_data"] is None
    assert captured["json_data"] is None
    assert "Content-Type" not in captured["headers"]
