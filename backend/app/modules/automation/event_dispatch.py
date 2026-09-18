from __future__ import annotations

from backend.app.core.database.connection import SessionLocal
from backend.app.core.execution_claims import execution_claims
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime


def _recorded_event_run(db, *, workflow_id: int, event_id: str) -> AutomationRun | None:
    rows = (
        db.query(AutomationRun)
        .filter(AutomationRun.workflow_id == int(workflow_id))
        .order_by(AutomationRun.id.desc())
        .limit(200)
        .all()
    )
    for row in rows:
        payload = row.input_data if isinstance(row.input_data, dict) else {}
        if str(payload.get("_xvond_event_id") or "") == str(event_id):
            return row
    return None


def dispatch_automation_event(
    *,
    company_id: int,
    event_name: str,
    event_id: str,
    payload: dict | None = None,
) -> dict:
    clean_event = str(event_name or "").strip().lower()
    clean_event_id = str(event_id or "").strip()
    if not clean_event:
        raise ValueError("Automation event name is required")
    if not clean_event_id or len(clean_event_id) > 200:
        raise ValueError("Automation event id is required")

    db = SessionLocal()
    try:
        company = db.query(Company).filter(Company.id == int(company_id)).first()
        if company is None or not company.active or str(company.lifecycle_status or "").lower() != "live":
            return {"event": clean_event, "matched": 0, "runs": []}

        workflows = (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.company_id == int(company_id),
                AutomationWorkflow.trigger_type == "event",
                AutomationWorkflow.enabled.is_(True),
            )
            .order_by(AutomationWorkflow.id.asc())
            .all()
        )

        runs = []
        for workflow in workflows:
            config = workflow.trigger_config if isinstance(workflow.trigger_config, dict) else {}
            if str(config.get("event_name") or "").strip().lower() != clean_event:
                continue
            generated_agent_id = int(config.get("_xvond_agent_id") or 0)
            if generated_agent_id:
                employee = (
                    db.query(AIAgent)
                    .filter(
                        AIAgent.id == generated_agent_id,
                        AIAgent.company_id == int(company_id),
                    )
                    .first()
                )
                if employee is None or not employee.enabled:
                    continue

            recorded = _recorded_event_run(
                db,
                workflow_id=workflow.id,
                event_id=clean_event_id,
            )
            if recorded is not None:
                runs.append(
                    {
                        "workflow_id": workflow.id,
                        "run_id": recorded.id,
                        "status": "already_recorded",
                        "run_status": recorded.status,
                    }
                )
                continue

            claim_key = f"automation_event:{workflow.id}:{clean_event_id}"
            if not execution_claims.claim(claim_key, ttl_seconds=86400):
                runs.append(
                    {
                        "workflow_id": workflow.id,
                        "status": "already_accepted",
                    }
                )
                continue

            input_data = dict(payload or {})
            input_data["_xvond_trigger"] = "event"
            input_data["_xvond_event_name"] = clean_event
            input_data["_xvond_event_id"] = clean_event_id
            input_data["_xvond_execution_key"] = (
                f"automation:event:{company_id}:{workflow.id}:{clean_event_id}"
            )

            try:
                run = automation_runtime.execute(
                    db=db,
                    company_id=int(company_id),
                    workflow=workflow,
                    input_data=input_data,
                )
            except Exception:
                recorded = _recorded_event_run(
                    db,
                    workflow_id=workflow.id,
                    event_id=clean_event_id,
                )
                if recorded is None:
                    execution_claims.release(claim_key)
                    raise
                runs.append(
                    {
                        "workflow_id": workflow.id,
                        "run_id": recorded.id,
                        "status": "failed",
                        "run_status": recorded.status,
                    }
                )
                continue

            runs.append(
                {
                    "workflow_id": workflow.id,
                    "run_id": run.id,
                    "status": run.status,
                }
            )

        return {
            "event": clean_event,
            "matched": len(runs),
            "runs": runs,
        }
    finally:
        db.close()
