from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_admin
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.self_service_policy import is_self_service_company
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.cycle import _add_month
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription


router = APIRouter(
    prefix="/customer/subscription",
    tags=["Customer Subscription"],
)


class SelfServiceSubscriptionRequest(BaseModel):
    plan_id: int


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _plan_data(plan: ServicePlan) -> dict:
    return {
        "id": plan.id,
        "service_code": plan.service_code,
        "tier": plan.tier,
        "name": plan.name,
        "monthly_price": plan.monthly_price,
        "currency": plan.currency,
        "limits": dict(plan.limits or {}),
        "enabled": bool(plan.enabled),
        "free": Decimal(str(plan.monthly_price or 0)) == 0,
    }


def _subscription_data(item: ServiceSubscription | None, plan: ServicePlan | None) -> dict | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "service_code": item.service_code,
        "status": item.status,
        "plan": _plan_data(plan) if plan is not None else None,
        "current_period_start": item.current_period_start,
        "current_period_end": item.current_period_end,
    }


def _self_service_company(db, current_user: User) -> Company:
    company = db.query(Company).filter(Company.id == current_user.company_id).first()
    if company is None:
        raise HTTPException(404, "Company not found")
    if not is_self_service_company(company):
        raise HTTPException(
            409,
            "Self-Service subscription selection is only available to Self-Service workspaces",
        )
    return company


@router.get("/ai-agents/plans")
def self_service_ai_agent_plans(
    current_user: User = Depends(require_customer_admin),
):
    db = SessionLocal()
    try:
        company = _self_service_company(db, current_user)
        plans = (
            db.query(ServicePlan)
            .filter(
                ServicePlan.service_code == "ai_agents",
                ServicePlan.enabled.is_(True),
            )
            .order_by(ServicePlan.monthly_price.asc(), ServicePlan.id.asc())
            .all()
        )
        subscription = (
            db.query(ServiceSubscription)
            .filter(
                ServiceSubscription.company_id == company.id,
                ServiceSubscription.service_code == "ai_agents",
            )
            .first()
        )
        current_plan = db.get(ServicePlan, subscription.plan_id) if subscription else None
        return {
            "plans": [_plan_data(item) for item in plans],
            "subscription": _subscription_data(subscription, current_plan),
            "online_payments_enabled": False,
        }
    finally:
        db.close()


@router.post("/ai-agents/request")
def request_self_service_ai_agent_subscription(
    data: SelfServiceSubscriptionRequest,
    current_user: User = Depends(require_customer_admin),
):
    db = SessionLocal()
    try:
        company = _self_service_company(db, current_user)
        plan = (
            db.query(ServicePlan)
            .filter(
                ServicePlan.id == data.plan_id,
                ServicePlan.service_code == "ai_agents",
                ServicePlan.enabled.is_(True),
            )
            .first()
        )
        if plan is None:
            raise HTTPException(404, "AI Employee plan not found")

        item = (
            db.query(ServiceSubscription)
            .filter(
                ServiceSubscription.company_id == company.id,
                ServiceSubscription.service_code == "ai_agents",
            )
            .with_for_update()
            .first()
        )
        now = _utcnow_naive()
        if (
            item is not None
            and item.status == "active"
            and item.current_period_start <= now < item.current_period_end
        ):
            raise HTTPException(
                409,
                "An active AI Employee subscription already exists. Plan changes require billing support.",
            )

        free = Decimal(str(plan.monthly_price or 0)) == 0
        status = "active" if free else "pending_payment"
        if item is None:
            item = ServiceSubscription(
                company_id=company.id,
                service_code="ai_agents",
                plan_id=plan.id,
                status=status,
                current_period_start=now,
                current_period_end=_add_month(now),
            )
            db.add(item)
            db.flush()
        else:
            item.plan_id = plan.id
            item.status = status
            item.current_period_start = now
            item.current_period_end = _add_month(now)

        audit_service.log(
            db=db,
            action=(
                "service_subscription.customer_activated_free"
                if free
                else "service_subscription.customer_requested"
            ),
            resource_type="service_subscription",
            resource_id=item.id,
            user_id=current_user.id,
            company_id=company.id,
            details={
                "service_code": "ai_agents",
                "plan_id": plan.id,
                "plan_tier": plan.tier,
                "monthly_price": str(plan.monthly_price),
                "currency": plan.currency,
                "status": status,
                "online_payment": False,
            },
        )
        db.commit()
        db.refresh(item)
        return {
            "status": status,
            "subscription": _subscription_data(item, plan),
            "requires_payment": not free,
            "message": (
                "AI Employee subscription activated"
                if free
                else "Subscription request saved. Payment or Xvond approval is required before AI execution is enabled."
            ),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
