from __future__ import annotations

from datetime import datetime
import hmac

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.modules.tools.action_request import _internal_slots
from backend.app.modules.tools.business_models import ActionRequest
from backend.app.modules.tools.generic_capability_runtime import (
    GenericCapabilityRuntimeError,
    execute_generic_capability,
    generic_capability_readiness,
)


router = APIRouter(prefix="/internal/workflow", tags=["Xvond Internal Workflow"])


class InternalWorkflowAction(BaseModel):
    company_id: int
    agent_id: int
    conversation_id: int | None = None
    action: str
    request_id: str
    data: dict


def _require_workflow_secret(value: str | None) -> None:
    expected = str(settings.N8N_SHARED_SECRET or "")
    received = str(value or "")
    if not expected or not received or not hmac.compare_digest(received, expected):
        raise HTTPException(401, "Unauthorized workflow request")


def _native_receipt(request: ActionRequest) -> dict:
    details = request.details or {}
    value = details.get("_xvond_native_execution")
    return dict(value) if isinstance(value, dict) else {}


@router.post("/xvond-internal")
def execute_xvond_internal(
    payload: InternalWorkflowAction,
    x_xvond_n8n_secret: str | None = Header(default=None),
):
    _require_workflow_secret(x_xvond_n8n_secret)
    action_type, _, operation = str(payload.action or "").rpartition(".")
    if operation not in {"check_availability", "execute", "cancel"} or not action_type:
        raise HTTPException(400, "Unsupported internal workflow action")

    data = payload.data or {}
    action_config = data.get("action_config") or {}
    if not isinstance(action_config, dict):
        raise HTTPException(400, "Invalid action configuration")
    if str(action_config.get("destination", {}).get("type") or "") != "xvond_internal":
        raise HTTPException(400, "Action is not routed to Xvond Internal")

    db = SessionLocal()
    try:
        adapter = str((action_config.get("destination") or {}).get("adapter") or "").strip()
        if operation == "check_availability":
            if adapter == "generic_capability":
                readiness = generic_capability_readiness(action_config)
                return {
                    "success": True,
                    "action": payload.action,
                    "request_id": payload.request_id,
                    "data": {
                        "available": bool(readiness.get("ready")),
                        "runtime": "generic_capability",
                        "reason": readiness.get("reason"),
                    },
                    "error": None,
                }
            details = data.get("details") or {}
            result = _internal_slots(
                db,
                {"company_id": payload.company_id, "agent_id": payload.agent_id},
                action_type,
                action_config,
                details,
            )
            return {
                "success": result.success,
                "action": payload.action,
                "request_id": payload.request_id,
                "data": result.data or {},
                "error": result.error,
            }

        action_request_id = data.get("request_id")
        if not action_request_id:
            raise HTTPException(400, "Internal execution requires action request id")
        request = (
            db.query(ActionRequest)
            .filter(
                ActionRequest.id == int(action_request_id),
                ActionRequest.company_id == payload.company_id,
                ActionRequest.agent_id == payload.agent_id,
                ActionRequest.action_type == action_type,
            )
            .first()
        )
        if request is None:
            raise HTTPException(404, "Business request not found")

        idempotency_key = str(data.get("idempotency_key") or payload.request_id or "").strip()
        if not idempotency_key:
            raise HTTPException(400, "Idempotency key is required")
        current = _native_receipt(request)
        if current.get("idempotency_key") == idempotency_key and current.get("operation") == operation:
            return {
                "success": True,
                "action": payload.action,
                "request_id": payload.request_id,
                "data": {"native_execution": current, "already_executed": True},
                "error": None,
            }

        runtime_result = None
        if adapter == "generic_capability":
            if operation == "cancel":
                return {
                    "success": False,
                    "action": payload.action,
                    "request_id": payload.request_id,
                    "data": {"runtime": "generic_capability"},
                    "error": "Generic capability cancellation requires an explicit compensating plan",
                }
            try:
                runtime_result = execute_generic_capability(
                    db,
                    company_id=payload.company_id,
                    agent_id=payload.agent_id,
                    action_type=action_type,
                    action_config=action_config,
                    details=data.get("details") or {},
                    idempotency_key=idempotency_key,
                )
            except GenericCapabilityRuntimeError as exc:
                return {
                    "success": False,
                    "action": payload.action,
                    "request_id": payload.request_id,
                    "data": {"runtime": "generic_capability"},
                    "error": str(exc),
                }

        if operation == "execute" and adapter != "generic_capability":
            availability = action_config.get("availability") or {}
            if str(availability.get("mode") or "none") == "xvond_schedule":
                availability_result = _internal_slots(
                    db,
                    {"company_id": payload.company_id, "agent_id": payload.agent_id},
                    action_type,
                    action_config,
                    request.details or {},
                )
                if not availability_result.success or not (availability_result.data or {}).get("available"):
                    return {
                        "success": False,
                        "action": payload.action,
                        "request_id": payload.request_id,
                        "data": availability_result.data or {},
                        "error": availability_result.error or "Requested time is no longer available",
                    }

        receipt = {
            "operation": operation,
            "state": "confirmed",
            "idempotency_key": idempotency_key,
            "executed_at": datetime.utcnow().isoformat(),
            "destination": "xvond_internal",
        }
        details = dict(request.details or {})
        details["_xvond_native_execution"] = receipt
        request.details = details
        db.commit()
        response_data = {"native_execution": receipt, "action_request_id": request.id}
        if runtime_result is not None:
            response_data["runtime_result"] = runtime_result
        return {
            "success": True,
            "action": payload.action,
            "request_id": payload.request_id,
            "data": response_data,
            "error": None,
        }
    finally:
        db.close()
