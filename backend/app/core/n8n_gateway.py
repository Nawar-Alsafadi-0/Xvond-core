from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from backend.app.core.config.settings import settings
from backend.app.core.error_safety import safe_error_metadata, safe_error_type

logger = logging.getLogger("xvond.n8n")


class N8NGatewayError(RuntimeError):
    pass


class N8NActionGateway:
    """Outbound gateway from Xvond Core to the self-hosted n8n workflow entrypoint."""

    def __init__(self) -> None:
        self.enabled = settings.N8N_ENABLED
        self.webhook_url = settings.N8N_WEBHOOK_URL
        self.shared_secret = settings.N8N_SHARED_SECRET
        self.timeout_seconds = settings.N8N_TIMEOUT_SECONDS
        self.max_retries = settings.N8N_MAX_RETRIES

    def configured(self) -> bool:
        return bool(self.enabled and self.webhook_url and self.shared_secret)

    @staticmethod
    def _safe_workflow_result(
        result: dict[str, Any],
        *,
        request_id: str,
        action: str,
    ) -> dict[str, Any]:
        """Prevent failed workflow payloads from entering prompts/audit storage.

        Successful workflow data is part of the configured business-operation
        contract. Failed workflow responses are untrusted provider output and may
        contain credentials, stack traces, request bodies or customer data, so
        only structural control-plane metadata is allowed to leave this boundary.
        """
        success = bool(result.get("success"))
        safe = {
            "success": success,
            "request_id": str(result.get("request_id") or request_id),
            "action": str(result.get("action") or action),
        }
        if success:
            safe["data"] = result.get("data")
            return safe

        raw_code = result.get("error_code") or result.get("code")
        if isinstance(raw_code, (str, int)):
            code = str(raw_code).strip()
            if code and len(code) <= 120 and all(
                char.isalnum() or char in {"_", "-", ".", ":"}
                for char in code
            ):
                safe["error_code"] = code
        safe["error"] = "Workflow execution failed"
        safe["data"] = None
        return safe

    def provision_channel(
        self,
        *,
        company_id: int,
        agent_id: int,
        channel_id: int,
        channel_type: str,
        connection_key: str,
        provider_type: str,
        provider_url: str,
        provider_secret: str,
        provider_config: dict[str, Any],
        provider_account_label: str | None = None,
    ) -> dict[str, Any]:
        """Write one managed-channel route into the isolated workflow plane.

        Provider credentials are transported to n8n but are never persisted in
        Xvond Core. The workflow plane owns encryption/storage and provider use.
        """
        return self.execute(
            company_id=company_id,
            agent_id=agent_id,
            action="channel.provision",
            data={
                "channel_id": channel_id,
                "channel_type": channel_type,
                "connection_key": connection_key,
                "provider_type": provider_type,
                "provider_url": provider_url,
                "provider_secret": provider_secret,
                "provider_config": provider_config,
                "provider_account_label": provider_account_label,
            },
            max_retries_override=0,
        )

    def deactivate_channel(
        self,
        *,
        company_id: int,
        agent_id: int,
        channel_id: int,
        connection_key: str,
    ) -> dict[str, Any]:
        """Remove one managed-channel route from the isolated workflow registry."""
        return self.execute(
            company_id=company_id,
            agent_id=agent_id,
            action="channel.deactivate",
            data={
                "channel_id": channel_id,
                "connection_key": connection_key,
            },
            # Registry deletion is idempotent, but Core does not blindly retry a
            # control-plane call whose outcome may be unknown. Reconciliation
            # persists cleanup-pending state and retries deliberately later.
            max_retries_override=0,
        )

    def execute(
        self,
        *,
        company_id: int,
        agent_id: int,
        action: str,
        data: dict[str, Any] | None = None,
        conversation_id: int | None = None,
        request_id: str | None = None,
        max_retries_override: int | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise N8NGatewayError("n8n workflow execution is disabled")
        if not self.webhook_url:
            raise N8NGatewayError("N8N_WEBHOOK_URL is not configured")
        if not self.shared_secret:
            raise N8NGatewayError("N8N_SHARED_SECRET is not configured")

        normalized_action = str(action or "").strip()
        if not normalized_action:
            raise N8NGatewayError("n8n action is required")

        request_id = str(request_id or uuid.uuid4())
        payload = {
            "request_id": request_id,
            "company_id": company_id,
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "action": normalized_action,
            "data": data or {},
        }
        headers = {
            "Content-Type": "application/json",
            "X-Xvond-N8N-Secret": self.shared_secret,
            "X-Xvond-Request-ID": request_id,
        }

        retry_count = self.max_retries if max_retries_override is None else max(0, int(max_retries_override))
        attempts = retry_count + 1
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
                    raise N8NGatewayError("n8n returned a non-object response")
                if str(result.get("request_id") or request_id) != request_id:
                    raise N8NGatewayError("n8n response request_id mismatch")
                return self._safe_workflow_result(
                    result,
                    request_id=request_id,
                    action=normalized_action,
                )
            except (httpx.HTTPError, ValueError, N8NGatewayError) as exc:
                last_error = exc
                logger.warning(
                    "n8n workflow call failed",
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

        # N8NGatewayError messages are authored by Xvond and contain no upstream
        # body/credential text. Preserve those safe contract errors for callers;
        # third-party/http errors remain reduced to their structural type.
        if isinstance(last_error, N8NGatewayError):
            raise N8NGatewayError(str(last_error)) from last_error
        raise N8NGatewayError(
            f"n8n workflow execution failed ({safe_error_type(last_error)})"
        ) from last_error


n8n_gateway = N8NActionGateway()
