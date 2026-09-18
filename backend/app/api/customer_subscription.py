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
from backend.app.modules.billing.payment_gateway import PaymentGatewayError, payment_gateway
from backend.app.modules.billing.service_models import ServiceCheckout, ServicePlan, ServiceSubscription


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



def _online_payment_gateway():
    gateway = payment_gateway()
    if gateway is None or not gateway.configured():
        return None
    return gateway


def _checkout_data(item: ServiceCheckout | None) -> dict | None:
    if item is None:
        return None
    return {
        "id": item.id,
        "provider": item.provider,
        "status": item.status,
        "checkout_url": item.checkout_url,
        "provider_transaction_id": item.provider_transaction_id,
    }


def _pending_checkout(db, subscription_id: int, plan_id: int) -> ServiceCheckout | None:
    return (
        db.query(ServiceCheckout)
        .filter(
            ServiceCheckout.service_subscription_id == subscription_id,
            ServiceCheckout.plan_id == plan_id,
            ServiceCheckout.status.in_(("pending", "draft", "ready")),
        )
        .order_by(ServiceCheckout.id.desc())
        .first()
    )


def _create_online_checkout(
    db,
    *,
    gateway,
    company: Company,
    subscription: ServiceSubscription,
    plan: ServicePlan,
) -> ServiceCheckout:
    existing = _pending_checkout(db, subscription.id, plan.id)
    if existing is not None and str(existing.checkout_url or "").strip():
        return existing

    result = gateway.create_checkout(
        company_id=company.id,
        service_subscription_id=subscription.id,
        plan_id=plan.id,
        plan_tier=plan.tier,
        service_code=subscription.service_code,
    )
    checkout = ServiceCheckout(
        company_id=company.id,
        service_subscription_id=subscription.id,
        plan_id=plan.id,
        provider=result["provider"],
        provider_transaction_id=result["transaction_id"],
        provider_subscription_id=result.get("subscription_id"),
        status=result.get("status") or "pending",
        checkout_url=result["checkout_url"],
        amount=plan.monthly_price,
        currency=plan.currency,
    )
    db.add(checkout)
    db.flush()
    return checkout

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
        gateway = _online_payment_gateway()
        checkout = (
            _pending_checkout(db, subscription.id, subscription.plan_id)
            if subscription is not None and subscription.status == "pending_payment"
            else None
        )
        return {
            "plans": [_plan_data(item) for item in plans],
            "subscription": _subscription_data(subscription, current_plan),
            "online_payments_enabled": gateway is not None,
            "payment_provider": gateway.provider if gateway is not None else None,
            "checkout": _checkout_data(checkout),
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
            and item.status == "pending_payment"
            and item.plan_id == plan.id
        ):
            gateway = _online_payment_gateway()
            checkout = _pending_checkout(db, item.id, plan.id)
            if checkout is None and gateway is not None:
                try:
                    checkout = _create_online_checkout(
                        db,
                        gateway=gateway,
                        company=company,
                        subscription=item,
                        plan=plan,
                    )
                    db.commit()
                    db.refresh(checkout)
                except PaymentGatewayError as exc:
                    db.rollback()
                    raise HTTPException(
                        503,
                        "Online checkout is temporarily unavailable",
                    ) from exc
            return {
                "status": "pending_payment",
                "subscription": _subscription_data(item, plan),
                "requires_payment": True,
                "online_payment": gateway is not None,
                "checkout": _checkout_data(checkout),
                "message": (
                    "Complete checkout to activate the AI Employee plan."
                    if checkout is not None
                    else "Subscription request is pending payment or Xvond approval."
                ),
            }
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

        gateway = None if free else _online_payment_gateway()
        checkout = None
        if not free and gateway is not None:
            try:
                checkout = _create_online_checkout(
                    db,
                    gateway=gateway,
                    company=company,
                    subscription=item,
                    plan=plan,
                )
            except PaymentGatewayError as exc:
                db.rollback()
                raise HTTPException(
                    503,
                    "Online checkout is temporarily unavailable",
                ) from exc

        audit_service.log(
            db=db,
            action=(
                "service_subscription.customer_activated_free"
                if free
                else "service_subscription.customer_checkout_created"
                if checkout is not None
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
                "online_payment": checkout is not None,
                "payment_provider": gateway.provider if gateway is not None else None,
            },
        )
        db.commit()
        db.refresh(item)
        return {
            "status": status,
            "subscription": _subscription_data(item, plan),
            "requires_payment": not free,
            "online_payment": checkout is not None,
            "checkout": _checkout_data(checkout),
            "message": (
                "AI Employee subscription activated"
                if free
                else "Complete checkout to activate the AI Employee plan."
                if checkout is not None
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
