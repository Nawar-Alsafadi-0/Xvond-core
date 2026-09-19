from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException

from backend.app.modules.tools.business_models import ActionRequest


UNRESOLVED_EXTERNAL_STATUSES = {"executing", "external_failed", "cancelling"}
RECONCILIATION_OUTCOMES = {"executed", "not_executed", "cancelled"}


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def public_action_details(value: dict | None) -> dict:
    """Hide Xvond runtime bookkeeping while preserving customer-owned action data."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if not str(key).startswith("_xvond_")
    }


def external_execution_view(item: ActionRequest) -> dict | None:
    details = item.details if isinstance(item.details, dict) else {}
    execution = details.get("_xvond_execution")
    if not isinstance(execution, dict):
        return None
    return {
        "state": execution.get("state"),
        "operation": execution.get("operation"),
        "updated_at": execution.get("updated_at"),
        "error": execution.get("error"),
        "reconciliation_required": item.status in UNRESOLVED_EXTERNAL_STATUSES,
    }


def reconcile_external_action_request(
    item: ActionRequest,
    *,
    outcome: str,
    note: str | None = None,
) -> ActionRequest:
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in RECONCILIATION_OUTCOMES:
        raise HTTPException(400, "Invalid reconciliation outcome")
    if item.status not in UNRESOLVED_EXTERNAL_STATUSES:
        raise HTTPException(409, "Operation does not need external reconciliation")

    details = dict(item.details or {})
    previous = details.get("_xvond_execution")
    previous = dict(previous) if isinstance(previous, dict) else {}
    now = _utcnow_naive().isoformat()

    details["_xvond_reconciliation"] = {
        "outcome": clean_outcome,
        "note": str(note or "").strip()[:1000] or None,
        "reconciled_at": now,
        "previous_state": previous,
    }

    if clean_outcome == "executed":
        details["_xvond_execution"] = {
            **previous,
            "state": "confirmed",
            "operation": "execute",
            "updated_at": now,
            "reconciled": True,
        }
        item.status = "confirmed"
    elif clean_outcome == "cancelled":
        details["_xvond_execution"] = {
            **previous,
            "state": "confirmed",
            "operation": "cancel",
            "updated_at": now,
            "reconciled": True,
        }
        item.status = "cancelled"
    else:
        details["_xvond_execution"] = {
            **previous,
            "state": "reconciled_not_executed",
            "updated_at": now,
            "reconciled": True,
        }
        item.status = "new"

    item.details = details
    return item
