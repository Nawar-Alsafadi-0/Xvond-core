from __future__ import annotations

from backend.app.core.database.connection import SessionLocal
from backend.app.core.execution_claims import execution_claims
from backend.app.models.company import Company
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime


def dispatch_automation_event(
    *,
    company_id: int,
    event_name: str,
    event_id: str,
    payload: dict | None = None,
) -> dict:
    clean_event = str(event_name or "").strip()
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
            if str(config.get("event_name") or "").strip() != clean_event:
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
                runs.append(
                    {
                        "workflow_id": workflow.id,
                        "status": "failed",
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
