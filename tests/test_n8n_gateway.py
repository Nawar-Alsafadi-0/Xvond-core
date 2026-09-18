import httpx
import pytest

from backend.app.core.n8n_gateway import N8NActionGateway, N8NGatewayError


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://n8n.example/webhook/xvond")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self):
        return self._payload


def configured_gateway() -> N8NActionGateway:
    gateway = N8NActionGateway()
    gateway.enabled = True
    gateway.webhook_url = "https://n8n.example/webhook/xvond"
    gateway.shared_secret = "test-shared-secret"
    gateway.timeout_seconds = 5
    gateway.max_retries = 0
    return gateway


def test_n8n_gateway_sends_stable_contract(monkeypatch):
    gateway = configured_gateway()
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(
            {
                "success": True,
                "request_id": json["request_id"],
                "action": json["action"],
                "data": {"booking_id": "BK-1"},
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = gateway.execute(
        company_id=12,
        agent_id=4,
        conversation_id=33,
        action="create_booking",
        data={"date": "2026-09-01", "time": "17:00"},
        request_id="req-123",
    )

    assert captured["url"] == gateway.webhook_url
    assert captured["headers"]["X-Xvond-N8N-Secret"] == "test-shared-secret"
    assert captured["headers"]["X-Xvond-Request-ID"] == "req-123"
    assert captured["json"] == {
        "request_id": "req-123",
        "company_id": 12,
        "agent_id": 4,
        "conversation_id": 33,
        "action": "create_booking",
        "data": {"date": "2026-09-01", "time": "17:00"},
    }
    assert result["success"] is True
    assert result["data"]["booking_id"] == "BK-1"


def test_n8n_gateway_redacts_failed_workflow_payload(monkeypatch):
    gateway = configured_gateway()
    secret = "SECRET_TOKEN customer private payload"

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {
                "success": False,
                "request_id": "req-fail",
                "action": "create_booking",
                "error": secret,
                "message": secret,
                "data": {"stack": secret},
                "error_code": "UPSTREAM_TIMEOUT",
            }
        ),
    )

    result = gateway.execute(
        company_id=1,
        agent_id=2,
        action="create_booking",
        request_id="req-fail",
    )

    assert result == {
        "success": False,
        "request_id": "req-fail",
        "action": "create_booking",
        "error_code": "UPSTREAM_TIMEOUT",
        "error": "Workflow execution failed",
        "data": None,
    }
    assert secret not in str(result)


def test_n8n_gateway_rejects_request_id_mismatch(monkeypatch):
    gateway = configured_gateway()

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {"success": True, "request_id": "wrong-request-id"}
        ),
    )

    with pytest.raises(N8NGatewayError, match="request_id mismatch"):
        gateway.execute(
            company_id=1,
            agent_id=2,
            action="send_email",
            request_id="expected-request-id",
        )


def test_n8n_gateway_is_off_by_default_when_not_enabled():
    gateway = configured_gateway()
    gateway.enabled = False

    with pytest.raises(N8NGatewayError, match="disabled"):
        gateway.execute(company_id=1, agent_id=2, action="send_email")


def test_n8n_gateway_can_disable_retries_for_durable_side_effects(monkeypatch):
    gateway = configured_gateway()
    gateway.max_retries = 3
    calls = {"count": 0}

    def fail_post(*args, **kwargs):
        calls["count"] += 1
        request = httpx.Request("POST", gateway.webhook_url)
        raise httpx.ConnectTimeout("timeout", request=request)

    monkeypatch.setattr(httpx, "post", fail_post)

    with pytest.raises(N8NGatewayError):
        gateway.execute(
            company_id=1,
            agent_id=2,
            action="channel.send",
            request_id="durable-send-1",
            max_retries_override=0,
        )

    assert calls["count"] == 1
