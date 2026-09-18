from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from backend.app.core.config.settings import settings
from backend.app.core.error_safety import safe_error_metadata, safe_error_type

logger = logging.getLogger("xvond.n8n.channels")


class N8NChannelGatewayError(RuntimeError):
    pass


class N8NChannelGateway:
    """Xvond-managed communication channel gateway backed by self-hosted n8n.

    Provider credentials and provider-specific workflows stay in the workflow
    plane. Xvond Core sends only normalized channel routing data.
    """

    def __init__(self) -> None:
        self.enabled = settings.N8N_ENABLED
        self.webhook_url = settings.N8N_CHANNEL_WEBHOOK_URL
        self.shared_secret = settings.N8N_SHARED_SECRET
        self.timeout_seconds = settings.N8N_TIMEOUT_SECONDS
        self.max_retries = settings.N8N_MAX_RETRIES

    def configured(self) -> bool:
        return bool(self.enabled and self.webhook_url and self.shared_secret)

    @staticmethod
    def _safe_result(
        result: dict[str, Any],
        *,
        request_id: str,
        action: str,
    ) -> dict[str, Any]:
        success = bool(result.get("success"))
        safe: dict[str, Any] = {
            "success": success,
            "request_id": str(result.get("request_id") or request_id),
            "action": str(result.get("action") or action),
        }
        data = result.get("data")
        if success and isinstance(data, dict):
            safe_data = {}
            for key in (
                "connected",
                "channel_id",
                "channel_type",
                "provider_message_id",
                "provider_reference",
                "route_key",
            ):
                value = data.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    safe_data[key] = value
            safe["data"] = safe_data
            return safe

        raw_code = result.get("error_code") or result.get("code")
        if isinstance(raw_code, (str, int)):
            code = str(raw_code).strip()
            if code and len(code) <= 120 and all(
                char.isalnum() or char in {"_", "-", ".", ":"}
                for char in code
            ):
                safe["error_code"] = code
        safe["error"] = "Channel workflow execution failed"
        safe["data"] = None
        return safe

    def execute(
        self,
        *,
        company_id: int,
        agent_id: int,
        channel_id: int,
        channel_type: str,
        action: str,
        external_contact_id: str | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise N8NChannelGatewayError("n8n channel execution is disabled")
        if not self.webhook_url:
            raise N8NChannelGatewayError("N8N_CHANNEL_WEBHOOK_URL is not configured")
        if not self.shared_secret:
            raise N8NChannelGatewayError("N8N_SHARED_SECRET is not configured")

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"channel.check", "channel.send"}:
            raise N8NChannelGatewayError("Unsupported n8n channel action")

        request_id = str(request_id or uuid.uuid4())
        data: dict[str, Any] = {
            "channel_id": int(channel_id),
            "channel_type": str(channel_type or "").strip().lower(),
        }
        if normalized_action == "channel.send":
            contact = str(external_contact_id or "").strip()
            text = str(message or "").strip()
            idem = str(idempotency_key or "").strip()
            if not contact or not text or not idem:
                raise N8NChannelGatewayError(
                    "Channel send requires contact, message and idempotency key"
                )
            data.update(
                {
                    "external_contact_id": contact,
                    "message": text,
                    "idempotency_key": idem,
                }
            )

        payload = {
            "request_id": request_id,
            "company_id": int(company_id),
            "agent_id": int(agent_id),
            "action": normalized_action,
            "data": data,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Xvond-N8N-Secret": self.shared_secret,
            "X-Xvond-Request-ID": request_id,
        }

        attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = httpx.post(
                    self.webhook_url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise N8NChannelGatewayError("n8n channel gateway returned a non-object response")
                if str(result.get("request_id") or request_id) != request_id:
                    raise N8NChannelGatewayError("n8n channel response request_id mismatch")
                return self._safe_result(
                    result,
                    request_id=request_id,
                    action=normalized_action,
                )
            except (httpx.HTTPError, ValueError, N8NChannelGatewayError) as exc:
                last_error = exc
                logger.warning(
                    "n8n channel workflow call failed",
                    extra={
                        "request_id": request_id,
                        "action": normalized_action,
                        "attempt": attempt,
                        "attempts": attempts,
                        **safe_error_metadata(exc),
                    },
                )
                if attempt < attempts:
                    time.sleep(min(0.25 * attempt, 1.0))

        if isinstance(last_error, N8NChannelGatewayError):
            raise N8NChannelGatewayError(str(last_error)) from last_error
        raise N8NChannelGatewayError(
            f"n8n channel workflow execution failed ({safe_error_type(last_error)})"
        ) from last_error

    def check_channel(
        self,
        *,
        company_id: int,
        agent_id: int,
        channel_id: int,
        channel_type: str,
    ) -> dict[str, Any]:
        return self.execute(
            company_id=company_id,
            agent_id=agent_id,
            channel_id=channel_id,
            channel_type=channel_type,
            action="channel.check",
        )

    def send_message(
        self,
        *,
        company_id: int,
        agent_id: int,
        channel_id: int,
        channel_type: str,
        external_contact_id: str,
        message: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.execute(
            company_id=company_id,
            agent_id=agent_id,
            channel_id=channel_id,
            channel_type=channel_type,
            action="channel.send",
            external_contact_id=external_contact_id,
            message=message,
            idempotency_key=idempotency_key,
        )


n8n_channel_gateway = N8NChannelGateway()
