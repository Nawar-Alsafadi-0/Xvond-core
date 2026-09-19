from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from backend.app.core.database.connection import SessionLocal
from backend.app.core.execution_claims import execution_claims
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime
from backend.app.modules.automation.webhook_auth import verify_automation_webhook_key


router = APIRouter(prefix="/webhooks/automation", tags=["Automation Webhooks"])


@router.post("/{workflow_id}")
async def automation_webhook(
    workflow_id: int,
    request: Request,
    x_xvond_webhook_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
):
    supplied_key = str(x_xvond_webhook_key or "").strip()
    execution_id = str(idempotency_key or "").strip()
    if not supplied_key:
        raise HTTPException(401, "Automation webhook key is required")
    if not execution_id or len(execution_id) > 200:
        raise HTTPException(400, "A stable Idempotency-Key is required")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Automation webhook payload must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Automation webhook payload must be a JSON object")

    db = SessionLocal()
    try:
        workflow = (
            db.query(AutomationWorkflow)
            .filter(
                AutomationWorkflow.id == int(workflow_id),
                AutomationWorkflow.trigger_type == "webhook",
                AutomationWorkflow.enabled.is_(True),
            )
            .first()
        )
        if workflow is None:
            raise HTTPException(404, "Automation webhook not found")

        company = db.query(Company).filter(Company.id == workflow.company_id).first()
        if company is None or not company.active or str(company.lifecycle_status or "").lower() != "live":
            raise HTTPException(409, "Automation company is not live")

        trigger_config = workflow.trigger_config if isinstance(workflow.trigger_config, dict) else {}
        generated_agent_id = int(trigger_config.get("_xvond_agent_id") or 0)
        if generated_agent_id:
            employee = (
                db.query(AIAgent)
                .filter(
                    AIAgent.id == generated_agent_id,
                    AIAgent.company_id == workflow.company_id,
                )
                .first()
            )
            if employee is None or not employee.enabled:
                raise HTTPException(409, "AI employee is not live")

        if not verify_automation_webhook_key(
            supplied_key,
            workflow_id=workflow.id,
            company_id=workflow.company_id,
        ):
            raise HTTPException(401, "Invalid automation webhook key")

        claim_key = f"automation_webhook:{workflow.id}:{execution_id}"
        if not execution_claims.claim(claim_key, ttl_seconds=86400):
            raise HTTPException(409, "This automation webhook event was already accepted")

        input_data = dict(body)
        input_data["_xvond_trigger"] = "webhook"
        input_data["_xvond_webhook_event_id"] = execution_id
        input_data["_xvond_execution_key"] = (
            f"automation:webhook:{workflow.company_id}:{workflow.id}:{execution_id}"
        )

        try:
            run = automation_runtime.execute(
                db=db,
                company_id=workflow.company_id,
                workflow=workflow,
                input_data=input_data,
            )
        except Exception:
            # The event claim deliberately remains held. Once an external event
            # begins executing, Xvond must not replay it automatically because a
            # downstream side effect may already have happened.
            raise

        return {
            "status": run.status,
            "workflow_id": workflow.id,
            "run_id": run.id,
        }
    finally:
        db.close()
