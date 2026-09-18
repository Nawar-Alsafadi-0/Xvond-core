from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.core.company_lifecycle import (
    CompanyNotFound,
    CompanyNotReady,
    InvalidCompanyLifecycle,
    activate_company,
    deactivate_company,
    set_company_lifecycle,
)
from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_admin, require_xvond_operator
from backend.app.core.n8n_gateway import n8n_gateway
from backend.app.core.password_policy import validate_password
from backend.app.core.security import hash_password
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.user import User
from backend.app.modules.audit.service import audit_service

router = APIRouter(prefix="/admin", tags=["Xvond Admin"])


class CompanyCreate(BaseModel):
    name: str
    owner_email: str
    owner_full_name: str
    owner_password: str


class CompanyStatusUpdate(BaseModel):
    active: bool


class CompanyLifecycleUpdate(BaseModel):
    status: str


@router.get("/workflow-engine/status")
def workflow_engine_status(current_admin: User = Depends(require_xvond_operator)):
    enabled = bool(settings.N8N_ENABLED)
    configured = bool(n8n_gateway.configured())
    if configured:
        status = "ready"
    elif enabled:
        status = "needs_setup"
    else:
        status = "disabled"
    return {
        "status": status,
        "enabled": enabled,
        "configured": configured,
        "webhook_configured": bool(settings.N8N_WEBHOOK_URL),
        "authentication_configured": bool(settings.N8N_SHARED_SECRET),
        "timeout_seconds": settings.N8N_TIMEOUT_SECONDS,
        "max_retries": settings.N8N_MAX_RETRIES,
    }


@router.post("/companies")
def create_company(data: CompanyCreate, current_admin: User = Depends(require_xvond_admin)):
    """Create an Xvond-managed onboarding tenant with portal access and runtime off."""
    name = data.name.strip()
    owner_email = data.owner_email.strip().lower()
    owner_full_name = data.owner_full_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name is required")
    if not owner_email:
        raise HTTPException(status_code=400, detail="Owner email is required")
    if not owner_full_name:
        raise HTTPException(status_code=400, detail="Owner full name is required")
    try:
        validate_password(data.owner_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    db = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.email == owner_email).first()
        if existing_user is not None:
            raise HTTPException(status_code=400, detail="Owner email already exists")
        company = Company(
            name=name,
            active=False,
            lifecycle_status="onboarding",
            onboarding_source="managed",
        )
        db.add(company)
        db.flush()
        owner = User(
            company_id=company.id,
            email=owner_email,
            full_name=owner_full_name,
            password_hash=hash_password(data.owner_password),
            role="owner",
        )
        db.add(owner)
        db.add(
            CompanyModule(
                company_id=company.id,
                module_name="ai_agent",
                enabled=True,
            )
        )
        audit_service.log(
            db=db,
            action="company.created",
            resource_type="company",
            resource_id=company.id,
            user_id=current_admin.id,
            company_id=company.id,
            details={
                "initial_state": "onboarding",
                "runtime_active": False,
                "billing_source": "service_subscriptions",
                "onboarding_source": "managed",
            },
        )
        db.commit()
        db.refresh(company)
        db.refresh(owner)
        return {
            "company": {
                "id": company.id,
                "name": company.name,
                "active": company.active,
                "lifecycle_status": company.lifecycle_status,
                "onboarding_source": company.onboarding_source,
            },
            "owner": {
                "id": owner.id,
                "company_id": owner.company_id,
                "email": owner.email,
                "full_name": owner.full_name,
                "role": owner.role,
            },
            "status": "created",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/companies/{company_id}/lifecycle")
def update_company_lifecycle(
    company_id: int,
    data: CompanyLifecycleUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    """Canonical commercial/onboarding lifecycle transition."""
    db = SessionLocal()
    try:
        previous = db.query(Company).filter(Company.id == company_id).first()
        previous_status = previous.lifecycle_status if previous is not None else None
        try:
            company, readiness = set_company_lifecycle(db, company_id, data.status)
        except CompanyNotFound as exc:
            raise HTTPException(status_code=404, detail="Company not found") from exc
        except InvalidCompanyLifecycle as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CompanyNotReady as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Company is not ready to go live",
                    "issues": exc.readiness["issues"],
                    "agents": exc.readiness["agents"],
                },
            ) from exc

        audit_service.log(
            db=db,
            action="company.lifecycle_changed",
            resource_type="company",
            resource_id=company.id,
            user_id=current_admin.id,
            company_id=company.id,
            details={
                "from": previous_status,
                "to": company.lifecycle_status,
                "runtime_active": company.active,
                "readiness_status": readiness.get("status") if readiness else None,
            },
        )
        db.commit()
        db.refresh(company)
        return {
            "status": "updated",
            "company": {
                "id": company.id,
                "name": company.name,
                "active": company.active,
                "lifecycle_status": company.lifecycle_status,
                "onboarding_source": company.onboarding_source,
                "lifecycle_updated_at": company.lifecycle_updated_at,
            },
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/companies/{company_id}/status")
def update_company_status(
    company_id: int,
    data: CompanyStatusUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    """Emergency runtime switch, separate from commercial lifecycle."""
    db = SessionLocal()
    try:
        try:
            if data.active:
                company, readiness = activate_company(db, company_id)
                action = "company.activated"
                details = {
                    "readiness_status": readiness.get("status"),
                    "ready_agents": [
                        item.get("id")
                        for item in readiness.get("agents", [])
                        if item.get("ready")
                    ],
                }
            else:
                company = deactivate_company(db, company_id)
                action = "company.deactivated"
                details = {"emergency_stop": True}
        except CompanyNotFound as exc:
            raise HTTPException(status_code=404, detail="Company not found") from exc
        except CompanyNotReady as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Company is not ready to activate",
                    "issues": exc.readiness["issues"],
                    "agents": exc.readiness["agents"],
                },
            ) from exc

        audit_service.log(
            db=db,
            action=action,
            resource_type="company",
            resource_id=company.id,
            user_id=current_admin.id,
            company_id=company.id,
            details=details,
        )
        db.commit()
        db.refresh(company)
        return {
            "status": "updated",
            "company": {
                "id": company.id,
                "name": company.name,
                "active": company.active,
                "lifecycle_status": company.lifecycle_status,
                "onboarding_source": company.onboarding_source,
            },
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/companies")
def list_companies(
    source: str | None = None,
    lifecycle: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    current_admin: User = Depends(require_xvond_operator),
):
    """List tenants with optional server-side filters for the admin control plane.

    Omitting every filter preserves the legacy behavior and returns the complete
    company list, so existing internal callers remain compatible.
    """
    normalized_source = str(source or "").strip().lower()
    if normalized_source and normalized_source not in {"managed", "self_service"}:
        raise HTTPException(status_code=400, detail="Invalid company source")

    normalized_lifecycle = str(lifecycle or "").strip().lower()
    allowed_lifecycle = {
        "onboarding",
        "testing",
        "live",
        "paused",
        "suspended",
        "cancelled",
        "archived",
    }
    if normalized_lifecycle and normalized_lifecycle not in allowed_lifecycle:
        raise HTTPException(status_code=400, detail="Invalid company lifecycle")

    safe_offset = max(0, int(offset or 0))
    safe_limit = None if limit is None else max(1, min(int(limit), 200))
    search_term = str(search or "").strip()

    db = SessionLocal()
    try:
        query = db.query(Company)
        if normalized_source:
            query = query.filter(Company.onboarding_source == normalized_source)
        if normalized_lifecycle:
            query = query.filter(Company.lifecycle_status == normalized_lifecycle)
        if search_term:
            query = query.filter(Company.name.ilike(f"%{search_term}%"))

        total = query.count()
        ordered = query.order_by(Company.id.asc())
        if safe_offset:
            ordered = ordered.offset(safe_offset)
        if safe_limit is not None:
            ordered = ordered.limit(safe_limit)
        companies = ordered.all()

        return {
            "companies": [
                {
                    "id": company.id,
                    "name": company.name,
                    "active": company.active,
                    "lifecycle_status": company.lifecycle_status,
                    "onboarding_source": company.onboarding_source,
                    "lifecycle_updated_at": company.lifecycle_updated_at,
                    "created_at": company.created_at,
                }
                for company in companies
            ],
            "total": total,
            "offset": safe_offset,
            "limit": safe_limit,
        }
    finally:
        db.close()
