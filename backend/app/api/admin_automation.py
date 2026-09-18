from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func

from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_admin
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.automation.models import AutomationRun, AutomationWorkflow
from backend.app.modules.automation.runtime import automation_runtime
from backend.app.modules.automation.schedule import ScheduleConfigError, normalize_schedule_config
from backend.app.modules.automation.webhook_auth import automation_webhook_key
from backend.app.modules.billing.service_limits import service_limits

router = APIRouter(prefix="/admin/automation", tags=["Xvond Admin - Automation"])

ALLOWED_TRIGGERS = {"manual", "webhook", "schedule", "event"}
IMPLEMENTED_TRIGGERS = {"manual", "schedule", "webhook", "event"}
ALLOWED_STEP_TYPES = {"ai", "tool", "condition", "webhook", "transform", "scheduled_action", "media_generation", "graph"}
TERMINAL_SIDE_EFFECT_TYPES = {"tool", "webhook", "scheduled_action"}
SCHEDULE_SAFE_STEP_TYPES = {"transform", "condition", "scheduled_action", "graph"}
WEBHOOK_SAFE_STEP_TYPES = {"transform", "condition", "scheduled_action", "graph"}
SENSITIVE_WORKFLOW_KEYS = {
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
}


class WorkflowCreate(BaseModel):
    name: str
    trigger_type: str = "manual"
    trigger_config: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list)


class WorkflowUpdate(BaseModel):
    name: str | None = None
    trigger_type: str | None = None
    trigger_config: dict | None = None
    steps: list[dict] | None = None
    enabled: bool | None = None


class WorkflowRunInput(BaseModel):
    input_data: dict = Field(default_factory=dict)


def require_company(db, company_id: int):
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _billing_service_for_workflow(item: AutomationWorkflow) -> str:
    source = str((item.trigger_config or {}).get("_xvond_source") or "").strip()
    return "ai_agents" if source == "self_service_employee" else "automation"


def _reject_inline_secrets(value, path: str = "workflow"):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in SENSITIVE_WORKFLOW_KEYS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Inline secret field '{key}' is not allowed in {path}. "
                        "Store credentials in an Integration instead."
                    ),
                )
            _reject_inline_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_inline_secrets(item, f"{path}[{index}]")


def validate_workflow(
    trigger_type: str,
    steps: list[dict],
    trigger_config: dict | None = None,
):
    trigger = (trigger_type or "").strip().lower()
    if trigger not in ALLOWED_TRIGGERS:
        raise HTTPException(status_code=400, detail="Unsupported automation trigger")

    _reject_inline_secrets(trigger_config or {}, "trigger_config")
    _reject_inline_secrets(steps or [], "steps")

    if trigger == "schedule":
        config = dict(trigger_config or {})
        raw_schedule = config.get("schedule")
        if not isinstance(raw_schedule, dict):
            raw_schedule = config
        try:
            normalize_schedule_config(raw_schedule)
        except ScheduleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    for index, step in enumerate(steps or []):
        if not isinstance(step, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Automation step {index} must be an object",
            )
        step_type = str(step.get("type", "")).strip().lower()
        if step_type not in ALLOWED_STEP_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported step type at {index}",
            )
        if trigger == "schedule" and step_type not in SCHEDULE_SAFE_STEP_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Scheduled step '{step_type}' is not production-safe yet. "
                    "Use the idempotent scheduled_action execution path."
                ),
            )
        if trigger == "webhook" and step_type not in WEBHOOK_SAFE_STEP_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Webhook step '{step_type}' is not production-safe yet. "
                    "Use a general execution graph or idempotent scheduled_action."
                ),
            )
        if step_type in TERMINAL_SIDE_EFFECT_TYPES and index != len(steps) - 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Side-effecting step '{step_type}' must be the final workflow "
                    "step so a later failure cannot create an unsafe replay."
                ),
            )
    return trigger


def serialize(item):
    return {
        "id": item.id,
        "company_id": item.company_id,
        "name": item.name,
        "trigger_type": item.trigger_type,
        "trigger_config": item.trigger_config,
        "steps": item.steps,
        "enabled": item.enabled,
        "trigger_runtime_ready": item.trigger_type in IMPLEMENTED_TRIGGERS,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


@router.get("/companies/{company_id}")
def list_workflows(
    company_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        require_company(db, company_id)
        items = (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.company_id == company_id)
            .order_by(AutomationWorkflow.id.desc())
            .all()
        )
        return {
            "company_id": company_id,
            "implemented_triggers": sorted(IMPLEMENTED_TRIGGERS),
            "workflows": [serialize(x) for x in items],
        }
    finally:
        db.close()


@router.post("/companies/{company_id}")
def create_workflow(
    company_id: int,
    data: WorkflowCreate,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        require_company(db, company_id)
        if settings.is_production:
            current = (
                db.query(func.count(AutomationWorkflow.id))
                .filter(AutomationWorkflow.company_id == company_id)
                .scalar()
                or 0
            )
            service_limits.check_current(
                db,
                company_id,
                "automation",
                "workflows",
                current,
            )
        name = data.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Workflow name is required")
        trigger = validate_workflow(
            data.trigger_type,
            data.steps,
            data.trigger_config,
        )
        item = AutomationWorkflow(
            company_id=company_id,
            name=name[:200],
            trigger_type=trigger,
            trigger_config=data.trigger_config or {},
            steps=data.steps or [],
            enabled=False,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return serialize(item)
    finally:
        db.close()


@router.patch("/{workflow_id}")
def update_workflow(
    workflow_id: int,
    data: WorkflowUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        item = (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.id == workflow_id)
            .first()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if data.name is not None:
            name = data.name.strip()
            if not name:
                raise HTTPException(400, "Workflow name cannot be empty")
            item.name = name[:200]

        new_trigger = (
            data.trigger_type if data.trigger_type is not None else item.trigger_type
        )
        new_steps = data.steps if data.steps is not None else item.steps
        new_trigger_config = (
            data.trigger_config
            if data.trigger_config is not None
            else item.trigger_config
        )
        item.trigger_type = validate_workflow(
            new_trigger,
            new_steps,
            new_trigger_config,
        )
        if data.trigger_config is not None:
            item.trigger_config = data.trigger_config
        if data.steps is not None:
            item.steps = data.steps
        if data.enabled is not None:
            if data.enabled and item.trigger_type not in IMPLEMENTED_TRIGGERS:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Trigger '{item.trigger_type}' is not production-enabled yet. "
                        "Keep this workflow disabled until its runtime dispatcher is connected."
                    ),
                )
            if data.enabled and settings.is_production:
                service_limits.entitlement(db, item.company_id, _billing_service_for_workflow(item))
            item.enabled = data.enabled
        db.commit()
        db.refresh(item)
        return serialize(item)
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/{workflow_id}/webhook-credentials")
def webhook_credentials(
    workflow_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        workflow = (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.id == workflow_id)
            .first()
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if workflow.trigger_type != "webhook":
            raise HTTPException(status_code=409, detail="Workflow is not webhook-triggered")
        if not settings.PUBLIC_BASE_URL:
            raise HTTPException(status_code=409, detail="PUBLIC_BASE_URL is not configured")
        return {
            "workflow_id": workflow.id,
            "url": f"{settings.PUBLIC_BASE_URL}/webhooks/automation/{workflow.id}",
            "header": "X-Xvond-Webhook-Key",
            "key": automation_webhook_key(
                workflow_id=workflow.id,
                company_id=workflow.company_id,
            ),
            "idempotency_header": "Idempotency-Key",
        }
    finally:
        db.close()


@router.post("/{workflow_id}/run")
def run_workflow(
    workflow_id: int,
    data: WorkflowRunInput,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        workflow = (
            db.query(AutomationWorkflow)
            .filter(AutomationWorkflow.id == workflow_id)
            .first()
        )
        if workflow is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if workflow.trigger_type != "manual":
            raise HTTPException(
                status_code=409,
                detail="Run now is only available for manual workflows; scheduled workflows use the scheduler dispatcher",
            )
        if settings.is_production:
            service_limits.entitlement(db, workflow.company_id, _billing_service_for_workflow(workflow))
        try:
            run = automation_runtime.execute(
                db=db,
                company_id=workflow.company_id,
                workflow=workflow,
                input_data=data.input_data,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "id": run.id,
            "workflow_id": run.workflow_id,
            "status": run.status,
            "output_data": run.output_data,
            "error_message": run.error_message,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }
    finally:
        db.close()


@router.get("/companies/{company_id}/runs")
def list_runs(
    company_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        require_company(db, company_id)
        items = (
            db.query(AutomationRun)
            .filter(AutomationRun.company_id == company_id)
            .order_by(AutomationRun.id.desc())
            .limit(200)
            .all()
        )
        return {
            "runs": [
                {
                    "id": x.id,
                    "workflow_id": x.workflow_id,
                    "status": x.status,
                    "created_at": x.created_at,
                    "finished_at": x.finished_at,
                    "error_message": x.error_message,
                }
                for x in items
            ]
        }
    finally:
        db.close()
