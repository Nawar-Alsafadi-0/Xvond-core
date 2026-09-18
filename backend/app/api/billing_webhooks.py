from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from backend.app.core.database.connection import SessionLocal
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.cycle import _add_month
from backend.app.modules.billing.payment_gateway import PaymentGatewayError, paddle_gateway
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentEvent,
    ServicePlan,
    ServiceSubscription,
)


router = APIRouter(prefix="/webhooks/billing", tags=["Billing Webhooks"])


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_provider_time(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _billing_period(data: dict) -> tuple[datetime, datetime]:
    period = data.get("billing_period")
    if not isinstance(period, dict):
        period = data.get("current_billing_period")
    period = period if isinstance(period, dict) else {}
    start = _parse_provider_time(period.get("starts_at"))
    end = _parse_provider_time(period.get("ends_at"))
    now = _utcnow_naive()
    if start is None:
        start = now
    if end is None or end <= start:
        end = _add_month(start)
    return start, end


def _checkout_for_transaction(db, transaction_id: str) -> ServiceCheckout | None:
    return (
        db.query(ServiceCheckout)
        .filter(
            ServiceCheckout.provider == "paddle",
            ServiceCheckout.provider_transaction_id == transaction_id,
        )
        .with_for_update()
        .first()
    )


def _checkout_for_subscription(db, provider_subscription_id: str) -> ServiceCheckout | None:
    return (
        db.query(ServiceCheckout)
        .filter(
            ServiceCheckout.provider == "paddle",
            ServiceCheckout.provider_subscription_id == provider_subscription_id,
        )
        .order_by(ServiceCheckout.id.desc())
        .with_for_update()
        .first()
    )


def _activate_transaction(db, data: dict) -> dict:
    transaction_id = str(data.get("id") or "").strip()
    if not transaction_id:
        raise HTTPException(400, "Payment transaction id is missing")

    checkout = _checkout_for_transaction(db, transaction_id)
    custom = data.get("custom_data") if isinstance(data.get("custom_data"), dict) else {}
    if checkout is None:
        subscription_id = custom.get("xvond_service_subscription_id")
        plan_id = custom.get("xvond_plan_id")
        company_id = custom.get("xvond_company_id")
        try:
            subscription_id = int(subscription_id)
            plan_id = int(plan_id)
            company_id = int(company_id)
        except (TypeError, ValueError):
            raise HTTPException(409, "Payment transaction is not linked to an Xvond checkout")

        subscription = db.get(ServiceSubscription, subscription_id)
        plan = db.get(ServicePlan, plan_id)
        if (
            subscription is None
            or plan is None
            or subscription.company_id != company_id
            or subscription.plan_id != plan_id
        ):
            raise HTTPException(409, "Payment transaction does not match the Xvond subscription")

        checkout = ServiceCheckout(
            company_id=company_id,
            service_subscription_id=subscription.id,
            plan_id=plan.id,
            provider="paddle",
            provider_transaction_id=transaction_id,
            provider_subscription_id=str(data.get("subscription_id") or "").strip() or None,
            status="completed",
            checkout_url=None,
            amount=plan.monthly_price,
            currency=plan.currency,
        )
        db.add(checkout)
        db.flush()
    else:
        subscription = db.get(ServiceSubscription, checkout.service_subscription_id)
        plan = db.get(ServicePlan, checkout.plan_id)
        if subscription is None or plan is None:
            raise HTTPException(409, "Xvond subscription for payment no longer exists")
        checkout.status = "completed"
        provider_subscription_id = str(data.get("subscription_id") or "").strip()
        if provider_subscription_id:
            checkout.provider_subscription_id = provider_subscription_id

    start, end = _billing_period(data)
    subscription.status = "active"
    subscription.plan_id = checkout.plan_id
    subscription.current_period_start = start
    subscription.current_period_end = end

    return {
        "subscription_id": subscription.id,
        "company_id": subscription.company_id,
        "plan_id": subscription.plan_id,
        "status": subscription.status,
        "transaction_id": transaction_id,
        "provider_subscription_id": checkout.provider_subscription_id,
    }


def _sync_subscription_state(db, data: dict) -> dict:
    provider_subscription_id = str(data.get("id") or "").strip()
    if not provider_subscription_id:
        raise HTTPException(400, "Payment subscription id is missing")

    checkout = _checkout_for_subscription(db, provider_subscription_id)
    if checkout is None:
        return {
            "ignored": True,
            "reason": "provider_subscription_not_linked",
            "provider_subscription_id": provider_subscription_id,
        }

    subscription = db.get(ServiceSubscription, checkout.service_subscription_id)
    if subscription is None:
        raise HTTPException(409, "Xvond subscription for payment no longer exists")

    provider_status = str(data.get("status") or "").strip().lower()
    if provider_status in {"canceled", "cancelled"}:
        subscription.status = "canceled"
        checkout.status = "canceled"
    elif provider_status in {"past_due", "paused"}:
        subscription.status = provider_status
        checkout.status = provider_status
    elif provider_status in {"active", "trialing"}:
        start, end = _billing_period(data)
        subscription.status = "active"
        subscription.current_period_start = start
        subscription.current_period_end = end
        checkout.status = "active"

    return {
        "subscription_id": subscription.id,
        "company_id": subscription.company_id,
        "status": subscription.status,
        "provider_subscription_id": provider_subscription_id,
    }


@router.post("/paddle")
async def paddle_billing_webhook(
    request: Request,
    paddle_signature: str | None = Header(default=None, alias="Paddle-Signature"),
):
    raw_body = await request.body()
    try:
        paddle_gateway.verify_webhook(
            raw_body=raw_body,
            signature_header=paddle_signature,
        )
    except PaymentGatewayError as exc:
        raise HTTPException(401, "Invalid payment webhook") from exc

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid payment webhook body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid payment webhook body")

    event_id = str(payload.get("event_id") or payload.get("notification_id") or "").strip()
    event_type = str(payload.get("event_type") or "").strip().lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if not event_id or not event_type:
        raise HTTPException(400, "Payment webhook identity is missing")

    db = SessionLocal()
    try:
        existing = (
            db.query(ServicePaymentEvent)
            .filter(
                ServicePaymentEvent.provider == "paddle",
                ServicePaymentEvent.provider_event_id == event_id,
            )
            .first()
        )
        if existing is not None:
            return {"status": "duplicate", "event_id": event_id}

        result: dict
        if event_type == "transaction.completed":
            result = _activate_transaction(db, data)
        elif event_type in {
            "subscription.created",
            "subscription.updated",
            "subscription.canceled",
        }:
            result = _sync_subscription_state(db, data)
        else:
            result = {"ignored": True, "event_type": event_type}

        evidence_checkout = None
        result_company_id = (
            result.get("company_id")
            if isinstance(result, dict)
            else None
        )
        if event_type == "transaction.completed":
            transaction_id = str(data.get("id") or "").strip()
            if transaction_id:
                evidence_checkout = _checkout_for_transaction(db, transaction_id)
        elif event_type.startswith("subscription."):
            provider_subscription_id = str(data.get("id") or "").strip()
            if provider_subscription_id:
                evidence_checkout = _checkout_for_subscription(
                    db,
                    provider_subscription_id,
                )

        db.add(
            ServicePaymentEvent(
                company_id=(
                    int(result_company_id)
                    if result_company_id is not None
                    else (
                        evidence_checkout.company_id
                        if evidence_checkout is not None
                        else None
                    )
                ),
                service_checkout_id=(
                    evidence_checkout.id
                    if evidence_checkout is not None
                    else None
                ),
                provider="paddle",
                provider_event_id=event_id,
                event_type=event_type,
            )
        )
        audit_service.log(
            db=db,
            action="billing.payment_webhook_processed",
            resource_type="billing",
            resource_id=None,
            company_id=result.get("company_id") if isinstance(result, dict) else None,
            details={
                "provider": "paddle",
                "event_id": event_id,
                "event_type": event_type,
                "result_status": result.get("status") if isinstance(result, dict) else None,
                "ignored": bool(result.get("ignored")) if isinstance(result, dict) else False,
            },
        )
        db.commit()
        return {"status": "processed", "event_id": event_id, "result": result}
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate", "event_id": event_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
