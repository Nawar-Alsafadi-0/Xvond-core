from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.company_catalog import normalize_currency
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_admin
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.cycle import _add_month
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.solutions.catalog import PACKAGE_TIERS, SERVICE_CATALOG

router = APIRouter(prefix="/admin/service-billing", tags=["Xvond Admin - Service Billing"])


class ServicePlanInput(BaseModel):
    service_code: str
    tier: str
    name: str
    monthly_price: Decimal = Field(ge=0)
    currency: str = "OMR"
    limits: dict = Field(default_factory=dict)


class ServicePlanUpdate(BaseModel):
    name: str | None = None
    monthly_price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    limits: dict | None = None
    enabled: bool | None = None


class ServiceSubscriptionInput(BaseModel):
    plan_id: int
    renew: bool = False


class ServiceStatusInput(BaseModel):
    status: str


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _plain_decimal(value) -> str:
    amount = Decimal(str(value))
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _validated_limits(value: dict | None) -> dict:
    result = {}
    for key, raw in (value or {}).items():
        metric = str(key or "").strip().lower()
        if not metric or len(metric) > 100:
            raise HTTPException(400, "Service limit metric is invalid")
        try:
            amount = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise HTTPException(400, f"Service limit '{metric}' must be numeric") from exc
        if amount < 0:
            raise HTTPException(400, "Service limits cannot be negative")
        result[metric] = _plain_decimal(amount) if amount else 0
    return result


def _serialized_limits(limits: dict | None) -> dict:
    result = {}
    for key, raw in (limits or {}).items():
        try:
            result[key] = _plain_decimal(raw)
        except (InvalidOperation, ValueError, TypeError):
            result[key] = raw
    return result


def plan_data(item):
    return {
        "id": item.id,
        "service_code": item.service_code,
        "tier": item.tier,
        "name": item.name,
        "monthly_price": item.monthly_price,
        "currency": item.currency,
        "limits": _serialized_limits(item.limits),
        "enabled": item.enabled,
    }


def _effective_status(item: ServiceSubscription) -> str:
    if item.status == "active" and item.current_period_end <= _utcnow_naive():
        return "expired"
    return item.status


def _audit_billing(
    db,
    admin: User,
    action: str,
    *,
    company_id: int | None = None,
    resource_type: str,
    resource_id: int,
    details: dict,
) -> None:
    audit_service.log(
        db=db,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=admin.id,
        company_id=company_id,
        details=details,
    )


@router.get("/plans")
def list_plans(current_admin: User = Depends(require_xvond_admin)):
    db = SessionLocal()
    try:
        items = db.query(ServicePlan).order_by(
            ServicePlan.service_code, ServicePlan.monthly_price
        ).all()
        return {"plans": [plan_data(item) for item in items]}
    finally:
        db.close()


@router.post("/plans")
def create_plan(data: ServicePlanInput, current_admin: User = Depends(require_xvond_admin)):
    service_code = data.service_code.strip().lower()
    tier = data.tier.strip().lower()
    if service_code not in SERVICE_CATALOG:
        raise HTTPException(status_code=400, detail="Unsupported service")
    if tier not in PACKAGE_TIERS:
        raise HTTPException(status_code=400, detail="Unsupported package tier")
    try:
        currency = normalize_currency(data.currency) or "OMR"
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    db = SessionLocal()
    try:
        duplicate = db.query(ServicePlan).filter(
            ServicePlan.service_code == service_code,
            ServicePlan.tier == tier,
        ).first()
        if duplicate:
            raise HTTPException(status_code=400, detail="Service plan already exists")
        item = ServicePlan(
            service_code=service_code,
            tier=tier,
            name=data.name.strip() or PACKAGE_TIERS[tier],
            monthly_price=data.monthly_price,
            currency=currency,
            limits=_validated_limits(data.limits),
            enabled=True,
        )
        db.add(item)
        db.flush()
        _audit_billing(
            db,
            current_admin,
            "service_plan.created",
            resource_type="service_plan",
            resource_id=item.id,
            details={"service_code": service_code, "tier": tier, "enabled": True},
        )
        db.commit()
        db.refresh(item)
        return plan_data(item)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/plans/{plan_id}")
def update_plan(
    plan_id: int,
    data: ServicePlanUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        item = db.get(ServicePlan, plan_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Service plan not found")

        changes = data.model_dump(exclude_unset=True)
        if "name" in changes:
            name = str(changes["name"] or "").strip()
            if not name:
                raise HTTPException(400, "Service plan name is required")
            item.name = name
        if "monthly_price" in changes:
            item.monthly_price = changes["monthly_price"]
        if "currency" in changes:
            try:
                item.currency = normalize_currency(changes["currency"]) or item.currency
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if "limits" in changes:
            item.limits = _validated_limits(changes["limits"])
        if "enabled" in changes:
            item.enabled = bool(changes["enabled"])

        _audit_billing(
            db,
            current_admin,
            "service_plan.updated",
            resource_type="service_plan",
            resource_id=item.id,
            details={
                "service_code": item.service_code,
                "tier": item.tier,
                "changed_fields": sorted(changes),
                "enabled": item.enabled,
            },
        )
        db.commit()
        db.refresh(item)
        return plan_data(item)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.put("/companies/{company_id}/services/{service_code}")
def subscribe_service(
    company_id: int,
    service_code: str,
    data: ServiceSubscriptionInput,
    current_admin: User = Depends(require_xvond_admin),
):
    service_code = service_code.strip().lower()
    if service_code not in SERVICE_CATALOG:
        raise HTTPException(status_code=400, detail="Unsupported service")
    db = SessionLocal()
    try:
        if db.get(Company, company_id) is None:
            raise HTTPException(status_code=404, detail="Company not found")
        plan = db.query(ServicePlan).filter(
            ServicePlan.id == data.plan_id,
            ServicePlan.service_code == service_code,
            ServicePlan.enabled.is_(True),
        ).first()
        if plan is None:
            raise HTTPException(status_code=404, detail="Service plan not found")

        now = _utcnow_naive()
        item = db.query(ServiceSubscription).filter(
            ServiceSubscription.company_id == company_id,
            ServiceSubscription.service_code == service_code,
        ).first()
        previous_plan_id = item.plan_id if item else None
        if item is None:
            item = ServiceSubscription(
                company_id=company_id,
                service_code=service_code,
                plan_id=plan.id,
                status="active",
                current_period_start=now,
                current_period_end=_add_month(now),
            )
            db.add(item)
            db.flush()
        else:
            previous_status = item.status
            period_expired = item.current_period_end <= now
            plan_changed = previous_plan_id != plan.id
            item.plan_id = plan.id
            item.status = "active"
            if plan_changed or data.renew or period_expired or previous_status in {"cancelled", "pending_payment"}:
                item.current_period_start = now
                item.current_period_end = _add_month(now)

        _audit_billing(
            db,
            current_admin,
            "service_subscription.assigned",
            company_id=company_id,
            resource_type="service_subscription",
            resource_id=item.id,
            details={
                "service_code": service_code,
                "plan_id": plan.id,
                "previous_plan_id": previous_plan_id,
                "renew": bool(data.renew),
            },
        )
        db.commit()
        db.refresh(item)
        return company_service_data(db, item, plan)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/companies/{company_id}/services/{service_code}/renew")
def renew_service(
    company_id: int,
    service_code: str,
    current_admin: User = Depends(require_xvond_admin),
):
    service_code = service_code.strip().lower()
    db = SessionLocal()
    try:
        item = db.query(ServiceSubscription).filter(
            ServiceSubscription.company_id == company_id,
            ServiceSubscription.service_code == service_code,
        ).first()
        if item is None:
            raise HTTPException(status_code=404, detail="Service subscription not found")
        plan = db.query(ServicePlan).filter(
            ServicePlan.id == item.plan_id,
            ServicePlan.enabled.is_(True),
            ServicePlan.service_code == service_code,
        ).first()
        if plan is None:
            raise HTTPException(status_code=409, detail="Cannot renew a service whose plan is unavailable")
        now = _utcnow_naive()
        item.status = "active"
        item.current_period_start = now
        item.current_period_end = _add_month(now)
        _audit_billing(
            db,
            current_admin,
            "service_subscription.renewed",
            company_id=company_id,
            resource_type="service_subscription",
            resource_id=item.id,
            details={"service_code": service_code, "plan_id": item.plan_id},
        )
        db.commit()
        db.refresh(item)
        return company_service_data(db, item, plan)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def company_service_data(db, item, plan=None):
    plan = plan or db.get(ServicePlan, item.plan_id)
    usage = {}
    for metric, limit in (plan.limits or {}).items():
        usage[metric] = {
            "used": service_limits.used(db, item, metric),
            "limit": _plain_decimal(limit) if limit not in (None, 0, "0") else 0,
        }
    return {
        "id": item.id,
        "company_id": item.company_id,
        "service_code": item.service_code,
        "service_name": SERVICE_CATALOG.get(item.service_code, {}).get(
            "name", item.service_code
        ),
        "status": _effective_status(item),
        "plan": plan_data(plan),
        "current_period_start": item.current_period_start,
        "current_period_end": item.current_period_end,
        "usage": usage,
    }


@router.get("/companies/{company_id}")
def company_services(company_id: int, current_admin: User = Depends(require_xvond_admin)):
    db = SessionLocal()
    try:
        if db.get(Company, company_id) is None:
            raise HTTPException(status_code=404, detail="Company not found")
        items = db.query(ServiceSubscription).filter(
            ServiceSubscription.company_id == company_id
        ).order_by(ServiceSubscription.service_code).all()
        return {
            "company_id": company_id,
            "services": [company_service_data(db, item) for item in items],
        }
    finally:
        db.close()


@router.patch("/companies/{company_id}/services/{service_code}/status")
def set_service_status(
    company_id: int,
    service_code: str,
    data: ServiceStatusInput,
    current_admin: User = Depends(require_xvond_admin),
):
    normalized_service_code = service_code.strip().lower()
    status = data.status.strip().lower()
    if status not in {"active", "paused", "cancelled"}:
        raise HTTPException(400, "Invalid service subscription status")
    db = SessionLocal()
    try:
        item = db.query(ServiceSubscription).filter(
            ServiceSubscription.company_id == company_id,
            ServiceSubscription.service_code == normalized_service_code,
        ).first()
        if item is None:
            raise HTTPException(status_code=404, detail="Service subscription not found")
        previous_status = item.status
        if status == "active":
            plan = db.query(ServicePlan).filter(
                ServicePlan.id == item.plan_id,
                ServicePlan.enabled.is_(True),
            ).first()
            if plan is None:
                raise HTTPException(409, "Cannot activate a service whose plan is disabled")
            now = _utcnow_naive()
            if item.current_period_end <= now or item.status in {"cancelled", "pending_payment"}:
                item.current_period_start = now
                item.current_period_end = _add_month(now)
        item.status = status
        if previous_status != status:
            _audit_billing(
                db,
                current_admin,
                "service_subscription.status_changed",
                company_id=company_id,
                resource_type="service_subscription",
                resource_id=item.id,
                details={
                    "service_code": normalized_service_code,
                    "previous_status": previous_status,
                    "status": status,
                },
            )
        db.commit()
        db.refresh(item)
        return company_service_data(db, item)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/companies/{company_id}/services/{service_code}/pause")
def pause_service(
    company_id: int,
    service_code: str,
    current_admin: User = Depends(require_xvond_admin),
):
    # Backward-compatible alias. New admin UI uses /status.
    return set_service_status(
        company_id,
        service_code,
        ServiceStatusInput(status="paused"),
        current_admin,
    )
