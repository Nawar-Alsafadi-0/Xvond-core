from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from backend.app.core.database.connection import SessionLocal
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime


MAX_EVENT_WORKFLOWS = 500


def _event_name(value: str) -> str:
    name = str(value or "").strip().lower()
    if not name or len(name) > 120:
        raise ValueError("Automation event name is invalid")
    return name


def _try_event_lock(db, *, workflow_id: int, event_id: str) -> bool:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return True
    value = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
        {"key": f"automation-event:{int(workflow_id)}:{event_id}"},
    ).scalar()
    return bool(value)


def _event_already_recorded(db, *, workflow_id: int, event_id: str) -> bool:
    rows = (
        db.query(AutomationRun)
        .filter(AutomationRun.workflow_id == int(workflow_id))
        .order_by(AutomationRun.id.desc())
        .limit(100)
        .all()
    )
    return any(
        str((row.input_data or {}).get("_xvond_event_id") or "") == event_id
        for row in rows
        if isinstance(row.input_data, dict)
    )


def dispatch_automation_event(
    *,
    company_id: int,
    event_name: str,
    payload: dict | None = None,
    event_id: str | None = None,
) -> dict:
    name = _event_name(event_name)
    body = dict(payload or {})
    if event_id is None:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        event_id = hashlib.sha256(
            f"{int(company_id)}:{name}:{canonical}".encode("utf-8")
        ).hexdigest()[:40]
    event_id = str(event_id or "").strip()
    if not event_id or len(event_id) > 200:
        raise ValueError("Automation event id is invalid")

    lookup = SessionLocal()
    try:
        candidates = (
            lookup.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.company_id == int(company_id),
                AutomationWorkflow.trigger_type == "event",
                AutomationWorkflow.enabled.is_(True),
            )
            .order_by(AutomationWorkflow.id.asc())
            .limit(MAX_EVENT_WORKFLOWS)
            .all()
        )
        workflow_ids = [
            workflow.id
            for workflow in candidates
            if str((workflow.trigger_config or {}).get("event_name") or "")
            .strip()
            .lower()
            == name
        ]
    finally:
        lookup.close()

    results: list[dict] = []
    for workflow_id in workflow_ids:
        db = SessionLocal()
        try:
            workflow = (
                db.query(AutomationWorkflow)
                .filter(
                    AutomationWorkflow.id == int(workflow_id),
                    AutomationWorkflow.company_id == int(company_id),
                    AutomationWorkflow.trigger_type == "event",
                    AutomationWorkflow.enabled.is_(True),
                )
                .first()
            )
            if workflow is None:
                continue
            if not _try_event_lock(db, workflow_id=workflow.id, event_id=event_id):
                db.rollback()
                results.append({"workflow_id": workflow.id, "status": "locked"})
                continue
            if _event_already_recorded(db, workflow_id=workflow.id, event_id=event_id):
                db.rollback()
                results.append({"workflow_id": workflow.id, "status": "already_recorded"})
                continue

            company = db.query(Company).filter(Company.id == int(company_id)).first()
            if company is None or not company.active or str(company.lifecycle_status or "").lower() != "live":
                db.rollback()
                results.append({"workflow_id": workflow.id, "status": "company_not_live"})
                continue

            trigger_config = dict(workflow.trigger_config or {})
            agent_id = int(trigger_config.get("_xvond_agent_id") or 0)
            if agent_id:
                employee = (
                    db.query(AIAgent)
                    .filter(
                        AIAgent.id == agent_id,
                        AIAgent.company_id == int(company_id),
                        AIAgent.enabled.is_(True),
                    )
                    .first()
                )
                if employee is None:
                    db.rollback()
                    results.append({"workflow_id": workflow.id, "status": "employee_not_live"})
                    continue

            input_data = dict(body)
            input_data["_xvond_trigger"] = "event"
            input_data["_xvond_event_name"] = name
            input_data["_xvond_event_id"] = event_id
            input_data["_xvond_execution_key"] = (
                f"automation:event:{int(company_id)}:{workflow.id}:{event_id}"
            )
            run = automation_runtime.execute(
                db=db,
                company_id=int(company_id),
                workflow=workflow,
                input_data=input_data,
            )
            results.append(
                {
                    "workflow_id": workflow.id,
                    "run_id": run.id,
                    "status": run.status,
                }
            )
        finally:
            db.close()

    return {
        "company_id": int(company_id),
        "event_name": name,
        "event_id": event_id,
        "matched": len(workflow_ids),
        "results": results,
    }
