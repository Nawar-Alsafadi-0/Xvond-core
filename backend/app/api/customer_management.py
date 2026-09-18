from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from backend.app.api.admin_ai_employee_files import upload_pdf_knowledge
from backend.app.api.admin_ai_employee_knowledge import (
    KnowledgeCreate,
    KnowledgeUpdate,
    WebsiteKnowledgeCreate,
    create_employee_knowledge,
    delete_employee_knowledge,
    get_employee_knowledge,
    ingest_website_knowledge,
    list_employee_knowledge,
    toggle_employee_knowledge,
    update_employee_knowledge,
)
from backend.app.api.admin_company_profile import (
    CompanyProfileUpdate,
    get_company_profile,
    update_company_profile,
)
from backend.app.core.dependencies import require_customer_manager
from backend.app.core.config_secrets import (
    configured_secret_fields,
    merge_config,
    public_config,
    reveal_config,
)
from backend.app.core.database.connection import SessionLocal
from backend.app.models.user import User
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.integrations.catalog import (
    get_integration_definition,
    list_integration_definitions,
    validate_integration_config,
)
from backend.app.modules.integrations.models import CompanyIntegration

router = APIRouter(prefix="/manage", tags=["Customer Manager Controls"])


class CustomerIntegrationCreate(BaseModel):
    integration_type: str
    name: str = Field(min_length=1, max_length=200)
    config: dict = Field(default_factory=dict)


class CustomerIntegrationUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    config: dict | None = None
    enabled: bool | None = None


def _serialize_integration(item: CompanyIntegration) -> dict:
    try:
        configured = validate_integration_config(
            item.integration_type,
            reveal_config(item.config),
        ) is True
    except ValueError:
        configured = False
    return {
        "id": item.id,
        "integration_type": item.integration_type,
        "name": item.name,
        "config": public_config(item.config),
        "configured_secret_fields": configured_secret_fields(item.config),
        "configured": configured,
        "enabled": bool(item.enabled),
        "created_at": item.created_at,
    }


def _company_id(user: User) -> int:
    if user.company_id is None:
        raise HTTPException(403, "Customer company required")
    return user.company_id


@router.get("/business-information")
def customer_business_information(
    current_user: User = Depends(require_customer_manager),
):
    return get_company_profile(
        company_id=_company_id(current_user),
        current_admin=current_user,
    )


@router.put("/business-information")
def update_customer_business_information(
    payload: CompanyProfileUpdate,
    current_user: User = Depends(require_customer_manager),
):
    return update_company_profile(
        company_id=_company_id(current_user),
        data=payload,
        current_admin=current_user,
    )


@router.get("/{agent_id}/knowledge")
def customer_knowledge_list(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return list_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        current_admin=current_user,
    )


@router.get("/{agent_id}/knowledge/{document_id}")
def customer_knowledge_item(
    agent_id: int,
    document_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return get_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        current_admin=current_user,
    )


@router.post("/{agent_id}/knowledge")
def customer_knowledge_create(
    agent_id: int,
    payload: KnowledgeCreate,
    current_user: User = Depends(require_customer_manager),
):
    return create_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        payload=payload,
        current_admin=current_user,
    )


@router.post("/{agent_id}/knowledge/url")
def customer_knowledge_url(
    agent_id: int,
    payload: WebsiteKnowledgeCreate,
    current_user: User = Depends(require_customer_manager),
):
    return ingest_website_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        payload=payload,
        current_admin=current_user,
    )


@router.post("/{agent_id}/knowledge/pdf")
async def customer_knowledge_pdf(
    agent_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(require_customer_manager),
):
    return await upload_pdf_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        file=file,
        current_admin=current_user,
    )


@router.put("/{agent_id}/knowledge/{document_id}")
def customer_knowledge_update(
    agent_id: int,
    document_id: int,
    payload: KnowledgeUpdate,
    current_user: User = Depends(require_customer_manager),
):
    return update_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        payload=payload,
        current_admin=current_user,
    )


@router.patch("/{agent_id}/knowledge/{document_id}/toggle")
def customer_knowledge_toggle(
    agent_id: int,
    document_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return toggle_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        current_admin=current_user,
    )


@router.delete("/{agent_id}/knowledge/{document_id}")
def customer_knowledge_delete(
    agent_id: int,
    document_id: int,
    current_user: User = Depends(require_customer_manager),
):
    return delete_employee_knowledge(
        company_id=_company_id(current_user),
        agent_id=agent_id,
        document_id=document_id,
        current_admin=current_user,
    )


@router.get("/integrations/catalog")
def customer_integration_catalog(
    current_user: User = Depends(require_customer_manager),
):
    _company_id(current_user)
    return {"integrations": list_integration_definitions()}


@router.get("/integrations")
def customer_integrations(
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _company_id(current_user)
        rows = (
            db.query(CompanyIntegration)
            .filter(CompanyIntegration.company_id == company_id)
            .order_by(CompanyIntegration.id.asc())
            .all()
        )
        return {"integrations": [_serialize_integration(item) for item in rows]}
    finally:
        db.close()


@router.post("/integrations")
def customer_integration_create(
    payload: CustomerIntegrationCreate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _company_id(current_user)
        integration_type = str(payload.integration_type or "").strip().lower()
        if get_integration_definition(integration_type) is None:
            raise HTTPException(400, "Unsupported integration type")
        name = str(payload.name or "").strip()
        if not name:
            raise HTTPException(400, "Integration name is required")
        try:
            validate_integration_config(integration_type, payload.config or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        # The AI Employee plan may include integration limits. If there is no
        # integration entitlement configured yet, the existing service layer
        # will fail closed in production rather than silently exceeding a plan.
        try:
            current = (
                db.query(CompanyIntegration)
                .filter(
                    CompanyIntegration.company_id == company_id,
                    CompanyIntegration.enabled.is_(True),
                )
                .count()
            )
            service_limits.check_current(
                db,
                company_id,
                "integrations",
                "integrations",
                current,
            )
        except HTTPException as exc:
            # Self-Service AI Employee plans can legitimately own connections
            # without a separate Integrations subscription. Only propagate
            # non-entitlement operational errors.
            if exc.status_code != 403:
                raise

        item = CompanyIntegration(
            company_id=company_id,
            integration_type=integration_type,
            name=name,
            config=payload.config or {},
            enabled=True,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return {"status": "created", **_serialize_integration(item)}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/integrations/{integration_id}")
def customer_integration_update(
    integration_id: int,
    payload: CustomerIntegrationUpdate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _company_id(current_user)
        item = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
            )
            .first()
        )
        if item is None:
            raise HTTPException(404, "Connected system not found")

        if payload.name is not None:
            name = str(payload.name or "").strip()
            if not name:
                raise HTTPException(400, "Integration name cannot be empty")
            item.name = name

        if payload.config is not None:
            merged = merge_config(item.config, payload.config)
            try:
                validate_integration_config(
                    item.integration_type,
                    reveal_config(merged),
                )
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            item.config = merged

        if payload.enabled is not None:
            item.enabled = bool(payload.enabled)

        db.commit()
        db.refresh(item)
        return {"status": "updated", **_serialize_integration(item)}
    except HTTPException:
        db.rollback()
        raise
    finally:
        db.close()


@router.delete("/integrations/{integration_id}")
def customer_integration_delete(
    integration_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        company_id = _company_id(current_user)
        item = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
            )
            .first()
        )
        if item is None:
            raise HTTPException(404, "Connected system not found")
        db.delete(item)
        db.commit()
        return {"status": "deleted", "integration_id": integration_id}
    finally:
        db.close()
