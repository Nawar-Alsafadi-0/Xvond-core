import httpx
import pytest

from backend.app.core.n8n_channel_gateway import (
    N8NChannelGateway,
    N8NChannelGatewayError,
)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://n8n.example/webhook/xvond-channels")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self):
        return self._payload


def configured_gateway() -> N8NChannelGateway:
    gateway = N8NChannelGateway()
    gateway.enabled = True
    gateway.webhook_url = "https://n8n.example/webhook/xvond-channels"
    gateway.shared_secret = "test-shared-secret"
    gateway.timeout_seconds = 5
    gateway.max_retries = 0
    return gateway


def test_n8n_channel_check_uses_normalized_xvond_contract(monkeypatch):
    gateway = configured_gateway()
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeResponse(
            {
                "success": True,
                "request_id": json["request_id"],
                "action": json["action"],
                "data": {
                    "connected": True,
                    "channel_id": 9,
                    "channel_type": "instagram",
                    "route_key": "12:9",
                    "should_not_escape": "provider-private",
                },
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = gateway.execute(
        company_id=12,
        agent_id=4,
        channel_id=9,
        channel_type="instagram",
        action="channel.check",
        request_id="req-check",
    )

    assert captured["json"] == {
        "request_id": "req-check",
        "company_id": 12,
        "agent_id": 4,
        "action": "channel.check",
        "data": {
            "channel_id": 9,
            "channel_type": "instagram",
        },
    }
    assert captured["headers"]["X-Xvond-N8N-Secret"] == "test-shared-secret"
    assert result["success"] is True
    assert result["data"] == {
        "connected": True,
        "channel_id": 9,
        "channel_type": "instagram",
        "route_key": "12:9",
    }


def test_n8n_channel_send_requires_stable_delivery_identity(monkeypatch):
    gateway = configured_gateway()
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["json"] = json
        return FakeResponse(
            {
                "success": True,
                "request_id": json["request_id"],
                "action": json["action"],
                "data": {
                    "connected": True,
                    "channel_id": 9,
                    "channel_type": "telegram",
                    "provider_message_id": "tg-55",
                },
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    result = gateway.send_message(
        company_id=1,
        agent_id=2,
        channel_id=9,
        channel_type="telegram",
        external_contact_id="customer-77",
        message="Hello",
        idempotency_key="msg-123",
    )

    assert captured["json"]["action"] == "channel.send"
    assert captured["json"]["data"] == {
        "channel_id": 9,
        "channel_type": "telegram",
        "external_contact_id": "customer-77",
        "message": "Hello",
        "idempotency_key": "msg-123",
    }
    assert result["data"]["provider_message_id"] == "tg-55"

    with pytest.raises(N8NChannelGatewayError, match="requires contact"):
        gateway.send_message(
            company_id=1,
            agent_id=2,
            channel_id=9,
            channel_type="telegram",
            external_contact_id="",
            message="Hello",
            idempotency_key="msg-124",
        )


def test_n8n_channel_gateway_redacts_failed_provider_payload(monkeypatch):
    gateway = configured_gateway()
    secret = "PRIVATE PROVIDER STACK AND TOKEN"

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {
                "success": False,
                "request_id": "req-fail",
                "action": "channel.send",
                "error": secret,
                "data": {"stack": secret},
                "error_code": "PROVIDER_TIMEOUT",
            }
        ),
    )

    result = gateway.execute(
        company_id=1,
        agent_id=2,
        channel_id=3,
        channel_type="email",
        action="channel.send",
        external_contact_id="customer@example.test",
        message="Hello",
        idempotency_key="delivery-1",
        request_id="req-fail",
    )

    assert result == {
        "success": False,
        "request_id": "req-fail",
        "action": "channel.send",
        "error_code": "PROVIDER_TIMEOUT",
        "error": "Channel workflow execution failed",
        "data": None,
    }
    assert secret not in str(result)


def test_n8n_channel_gateway_rejects_request_id_mismatch(monkeypatch):
    gateway = configured_gateway()
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: FakeResponse(
            {"success": True, "request_id": "wrong", "action": "channel.check"}
        ),
    )

    with pytest.raises(N8NChannelGatewayError, match="request_id mismatch"):
        gateway.check_channel(
            company_id=1,
            agent_id=2,
            channel_id=3,
            channel_type="instagram",
        )
