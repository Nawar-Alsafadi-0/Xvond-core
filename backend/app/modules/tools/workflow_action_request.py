from __future__ import annotations

from copy import deepcopy

from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.modules.tools.action_request import (
    ActionRequestTool,
    _action,
    _customer_confirmed,
    _customer_details,
    _execution_state,
    _latest_user_message,
    _missing,
    _save_execution_state,
    _summary,
)
from backend.app.modules.tools.base import ToolResult
from backend.app.modules.tools.business_models import ActionRequest


_SENSITIVE_CONFIG_PARTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "cookie",
    "private_key",
    "client_secret",
)


def _workflow_safe_config(value):
    """Return workflow-routing metadata without Xvond-side credentials.

    Action configuration may contain legacy integration credentials or arbitrary
    provider settings. The execution plane must receive routing metadata only;
    provider credentials belong to the workflow engine itself.
    """
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in {"headers", "auth", "authentication"}:
                continue
            if any(part in normalized for part in _SENSITIVE_CONFIG_PARTS):
                continue
            cleaned[key] = _workflow_safe_config(item)
        return cleaned
    if isinstance(value, list):
        return [_workflow_safe_config(item) for item in value]
    if isinstance(value, tuple):
        return [_workflow_safe_config(item) for item in value]
    return deepcopy(value)


def _workflow_payload(result: dict) -> dict:
    """Keep provider data backward-compatible while preserving workflow identity."""
    inner = result.get("data")
    workflow_meta = {
        "request_id": result.get("request_id"),
        "action": result.get("action"),
    }
    if isinstance(inner, dict):
        payload = dict(inner)
        payload["_workflow"] = workflow_meta
        return payload
    return {"result": inner, "_workflow": workflow_meta}


class WorkflowActionRequestTool(ActionRequestTool):
    """Authoritative business-action tool backed by the workflow engine.

    Xvond remains the control plane: it validates scope, collects customer data,
    records request state and decides which action is allowed. The external
    workflow engine is the only execution plane for availability checks,
    execution and cancellation.
    """

    description = (
        "Run configured business actions through the Xvond Workflow Engine. "
        "Xvond validates and tracks the request, while the workflow engine performs "
        "availability checks, bookings, orders, CRM/POS/ERP/API work, notifications, "
        "cancellations and other operational side effects."
    )

    @staticmethod
    def _workflow_result(
        *,
        context: dict,
        action_type: str,
        operation: str,
        data: dict,
        request_id: str | None = None,
    ) -> ToolResult:
        try:
            result = n8n_gateway.execute(
                company_id=context["company_id"],
                agent_id=context["agent_id"],
                conversation_id=context.get("conversation_id"),
                action=f"{action_type}.{operation}",
                data=data,
                request_id=request_id,
            )
        except N8NGatewayError as exc:
            return ToolResult(success=False, error=str(exc))

        success = bool(result.get("success"))
        return ToolResult(
            success=success,
            data=_workflow_payload(result),
            error=None if success else str(result.get("error") or result.get("message") or "Workflow execution failed"),
        )

    def execute(self, arguments, context):
        operation = str(arguments.get("operation") or "").strip()
        action_type = str(arguments.get("action_type") or "").strip()
        config = context.get("config", {}) or {}
        action = _action(config, action_type)
        if action is None:
            return ToolResult(
                success=False,
                error="This business action is not configured or enabled",
            )

        destination = action.get("destination") or {}
        # Xvond-native capabilities (for example the built-in booking system)
        # stay inside the Core control/runtime path. Existing external systems
        # continue through the workflow engine.
        if str(destination.get("type") or "").strip() in {"xvond_internal", "integration"}:
            return super().execute(arguments, context)

        if operation == "check_availability":
            details = arguments.get("details") or {}
            if not isinstance(details, dict):
                return ToolResult(success=False, error="Action details must be structured")
            return self._workflow_result(
                context=context,
                action_type=action_type,
                operation="check_availability",
                data={
                    "operation": "check_availability",
                    "action_type": action_type,
                    "details": details,
                    "action_config": _workflow_safe_config(action),
                },
            )

        if operation == "prepare":
            return self._prepare_request(arguments, context, action_type, action)

        # status is control-plane state only. execute/cancel are resolved by the
        # parent dispatcher, which calls the workflow-backed overrides below.
        return super().execute(arguments, context)

    def _prepare_request(
        self,
        arguments: dict,
        context: dict,
        action_type: str,
        action: dict,
    ) -> ToolResult:
        db = context["db"]
        details = arguments.get("details") or {}
        if not isinstance(details, dict):
            return ToolResult(success=False, error="Action details must be structured")

        missing = _missing(action, details)
        if missing:
            return ToolResult(
                success=False,
                error="Missing required customer details: " + ", ".join(missing),
                data={"missing_fields": missing},
            )

        destination = action.get("destination") or {}
        if str(destination.get("type") or "unconfigured").strip() == "unconfigured":
            return ToolResult(
                success=False,
                error="This action has no workflow destination configured",
            )

        summary = _summary(action, details, arguments.get("summary"))
        existing = None
        if context.get("conversation_id") is not None:
            existing = (
                db.query(ActionRequest)
                .filter(
                    ActionRequest.company_id == context["company_id"],
                    ActionRequest.agent_id == context["agent_id"],
                    ActionRequest.conversation_id == context["conversation_id"],
                    ActionRequest.action_type == action_type,
                    ActionRequest.status == "awaiting_confirmation",
                )
                .order_by(ActionRequest.id.desc())
                .first()
            )

        if existing is not None:
            request = existing
            request.details = dict(details)
            request.summary = summary
        else:
            request = ActionRequest(
                company_id=context["company_id"],
                agent_id=context["agent_id"],
                conversation_id=context.get("conversation_id"),
                action_type=action_type,
                details=dict(details),
                summary=summary,
                status="awaiting_confirmation",
            )
            db.add(request)
            db.flush()

        if not bool(action.get("confirmation_required", True)):
            return self._execute_request(request, action, arguments, context)

        return ToolResult(
            success=True,
            data={
                "action": "prepared",
                "request_id": request.id,
                "status": request.status,
                "summary": request.summary,
                "details": request.details,
                "confirmation_required": True,
            },
        )

    def _execute_request(
        self,
        request: ActionRequest,
        action: dict,
        arguments: dict,
        context: dict,
    ) -> ToolResult:
        db = context["db"]
        execution = _execution_state(request)
        if (
            request.status == "confirmed"
            and execution.get("operation") == "execute"
            and execution.get("state") == "confirmed"
        ):
            return ToolResult(
                success=True,
                data={
                    "action": "executed",
                    "request_id": request.id,
                    "status": request.status,
                    "already_executed": True,
                    "execution": execution,
                },
            )
        if request.status in {"executing", "external_failed", "cancelling"}:
            return ToolResult(
                success=False,
                error="This workflow request has an unresolved execution state and must be reconciled before retrying.",
                data={"request_id": request.id, "execution": execution},
            )
        if request.status not in {"awaiting_confirmation", "new"}:
            return ToolResult(
                success=False,
                error=f"Request cannot be executed from status {request.status}",
            )
        if bool(action.get("confirmation_required", True)):
            latest = _latest_user_message(db, context.get("conversation_id"))
            if not _customer_confirmed(latest):
                return ToolResult(
                    success=False,
                    error="Customer confirmation is required before execution",
                )

        idempotency_key = f"xvond-action-{context['company_id']}-{request.id}-execute-v1"
        _save_execution_state(
            request,
            state="executing",
            key=idempotency_key,
            operation="execute",
        )
        request.status = "executing"
        db.commit()
        db.refresh(request)

        result = self._workflow_result(
            context=context,
            action_type=request.action_type,
            operation="execute",
            request_id=idempotency_key,
            data={
                "operation": "execute",
                "request_id": request.id,
                "action_type": request.action_type,
                "details": _customer_details(request.details or {}),
                "summary": request.summary,
                "action_config": _workflow_safe_config(action),
                "idempotency_key": idempotency_key,
            },
        )
        if not result.success:
            _save_execution_state(
                request,
                state="external_failed",
                key=idempotency_key,
                operation="execute",
                error=result.error,
                result=result.data,
            )
            request.status = "external_failed"
            db.commit()
            return result

        _save_execution_state(
            request,
            state="confirmed",
            key=idempotency_key,
            operation="execute",
            result=result.data,
        )
        request.status = "confirmed"
        meta = dict(request.details or {})
        meta["_xvond_destination"] = {"type": "workflow_engine"}
        request.details = meta
        db.commit()
        return ToolResult(
            success=True,
            data={
                "action": "executed",
                "request_id": request.id,
                "status": request.status,
                "workflow_result": result.data,
            },
        )

    def _cancel_request(
        self,
        request: ActionRequest,
        action: dict,
        context: dict,
    ) -> ToolResult:
        db = context["db"]
        if request.status == "cancelled":
            return ToolResult(
                success=True,
                data={
                    "request_id": request.id,
                    "status": "cancelled",
                    "already_cancelled": True,
                },
            )

        current = _execution_state(request)
        if current.get("operation") == "cancel" and current.get("state") == "confirmed":
            request.status = "cancelled"
            return ToolResult(
                success=True,
                data={
                    "request_id": request.id,
                    "status": request.status,
                    "already_cancelled": True,
                },
            )
        if current.get("state") in {"executing", "external_failed"}:
            return ToolResult(
                success=False,
                error="Workflow execution is unresolved. Reconcile this request before cancellation.",
                data={"request_id": request.id, "execution": current},
            )

        idempotency_key = f"xvond-action-{context['company_id']}-{request.id}-cancel-v1"
        _save_execution_state(
            request,
            state="executing",
            key=idempotency_key,
            operation="cancel",
        )
        request.status = "cancelling"
        db.commit()
        db.refresh(request)

        result = self._workflow_result(
            context=context,
            action_type=request.action_type,
            operation="cancel",
            request_id=idempotency_key,
            data={
                "operation": "cancel",
                "request_id": request.id,
                "action_type": request.action_type,
                "details": _customer_details(request.details or {}),
                "summary": request.summary,
                "action_config": _workflow_safe_config(action),
                "idempotency_key": idempotency_key,
            },
        )
        if not result.success:
            _save_execution_state(
                request,
                state="external_failed",
                key=idempotency_key,
                operation="cancel",
                error=result.error,
                result=result.data,
            )
            request.status = "external_failed"
            db.commit()
            return result

        _save_execution_state(
            request,
            state="confirmed",
            key=idempotency_key,
            operation="cancel",
            result=result.data,
        )
        request.status = "cancelled"
        db.commit()
        return ToolResult(
            success=True,
            data={
                "request_id": request.id,
                "status": request.status,
                "workflow_result": result.data,
            },
        )


workflow_action_request_tool = WorkflowActionRequestTool()
