from __future__ import annotations

from datetime import UTC, datetime
import logging

from sqlalchemy import text

from backend.app.core.database.connection import SessionLocal
from backend.app.models.company import Company
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime
from backend.app.modules.automation.schedule import (
    ScheduleConfigError,
    latest_due_slot,
    normalize_schedule_config,
    schedule_slot_key,
)


logger = logging.getLogger("xvond.automation.scheduler")
MAX_RECENT_RUNS_FOR_DEDUP = 50


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _schedule_payload(workflow: AutomationWorkflow) -> dict:
    config = dict(workflow.trigger_config or {})
    nested = config.get("schedule")
    if isinstance(nested, dict):
        return dict(nested)
    return {
        key: value
        for key, value in config.items()
        if key in {"kind", "every_minutes", "anchor_at", "hour", "minute", "timezone", "weekdays"}
    }


def _try_workflow_lock(db, workflow_id: int) -> bool:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return True
    value = db.execute(
        text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
        {"key": f"automation-schedule:{int(workflow_id)}"},
    ).scalar()
    return bool(value)


def _slot_already_recorded(db, workflow_id: int, slot_key: str) -> bool:
    rows = (
        db.query(AutomationRun)
        .filter(AutomationRun.workflow_id == workflow_id)
        .order_by(AutomationRun.id.desc())
        .limit(MAX_RECENT_RUNS_FOR_DEDUP)
        .all()
    )
    for row in rows:
        payload = row.input_data if isinstance(row.input_data, dict) else {}
        if str(payload.get("_xvond_schedule_slot") or "") == slot_key:
            return True
    return False


def run_due_workflow(workflow_id: int, *, now: datetime | None = None) -> dict:
    db = SessionLocal()
    try:
        workflow = (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.id == int(workflow_id),
                AutomationWorkflow.trigger_type == "schedule",
                AutomationWorkflow.enabled.is_(True),
            )
            .first()
        )
        if workflow is None:
            return {"workflow_id": int(workflow_id), "status": "inactive"}

        if not _try_workflow_lock(db, workflow.id):
            db.rollback()
            return {"workflow_id": workflow.id, "status": "locked"}

        company = db.query(Company).filter(Company.id == workflow.company_id).first()
        if company is None:
            db.rollback()
            return {"workflow_id": workflow.id, "status": "company_missing"}
        if not company.active or str(company.lifecycle_status or "").lower() != "live":
            db.rollback()
            return {"workflow_id": workflow.id, "status": "company_not_live"}

        raw_schedule = _schedule_payload(workflow)
        try:
            schedule = normalize_schedule_config(raw_schedule)
        except ScheduleConfigError as exc:
            db.rollback()
            return {
                "workflow_id": workflow.id,
                "status": "invalid_schedule",
                "error": str(exc),
            }

        current = now or _utcnow()
        slot = latest_due_slot(
            schedule,
            now=current,
            created_at=workflow.created_at,
        )
        if slot is None:
            db.rollback()
            return {"workflow_id": workflow.id, "status": "not_due"}

        slot_key = schedule_slot_key(slot)
        if _slot_already_recorded(db, workflow.id, slot_key):
            db.rollback()
            return {
                "workflow_id": workflow.id,
                "status": "already_recorded",
                "slot": slot_key,
            }

        trigger_config = dict(workflow.trigger_config or {})
        input_data = trigger_config.get("input_data")
        if not isinstance(input_data, dict):
            input_data = {}
        input_data = dict(input_data)
        input_data["_xvond_trigger"] = "schedule"
        input_data["_xvond_schedule_slot"] = slot_key
        input_data["_xvond_execution_key"] = (
            f"automation:{workflow.company_id}:{workflow.id}:{slot_key}"
        )

        run = automation_runtime.execute(
            db=db,
            company_id=workflow.company_id,
            workflow=workflow,
            input_data=input_data,
        )
        return {
            "workflow_id": workflow.id,
            "status": run.status,
            "run_id": run.id,
            "slot": slot_key,
        }
    finally:
        db.close()


def run_due_schedules_once(*, now: datetime | None = None, limit: int = 200) -> dict:
    db = SessionLocal()
    try:
        workflow_ids = [
            row[0]
            for row in (
                db.query(AutomationWorkflow.id)
                .filter(
                    AutomationWorkflow.trigger_type == "schedule",
                    AutomationWorkflow.enabled.is_(True),
                )
                .order_by(AutomationWorkflow.id.asc())
                .limit(max(1, min(int(limit), 1000)))
                .all()
            )
        ]
    finally:
        db.close()

    results = []
    for workflow_id in workflow_ids:
        try:
            results.append(run_due_workflow(workflow_id, now=now))
        except Exception:
            logger.exception(
                "Scheduled automation execution failed",
                extra={"workflow_id": workflow_id},
            )
            results.append({"workflow_id": workflow_id, "status": "failed"})

    executed = sum(1 for item in results if item.get("status") == "success")
    failed = sum(1 for item in results if item.get("status") == "failed")
    return {
        "checked": len(results),
        "executed": executed,
        "failed": failed,
        "results": results,
    }
