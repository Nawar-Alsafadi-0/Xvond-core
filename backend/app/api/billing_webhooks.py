from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from backend.app.core.database.connection import SessionLocal
from backend.app.core.config_secrets import protect_config
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.cycle import _add_month
from backend.app.modules.billing.payment_gateway import (
    PaymentGatewayError,
    paddle_gateway,
    tap_gateway,
)
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentEvent,
    ServicePaymentProfile,
    ServicePlan,
    ServiceRenewalAttempt,
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


def _checkout_for_transaction(
    db,
    transaction_id: str,
    *,
    provider: str = "paddle",
) -> ServiceCheckout | None:
    return (
        db.query(ServiceCheckout)
        .filter(
            ServiceCheckout.provider == provider,
            ServiceCheckout.provider_transaction_id == transaction_id,
        )
        .with_for_update()
        .first()
    )


def _checkout_for_subscription(
    db,
    provider_subscription_id: str,
    *,
    provider: str = "paddle",
) -> ServiceCheckout | None:
    return (
        db.query(ServiceCheckout)
        .filter(
            ServiceCheckout.provider == provider,
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




def _tap_checkout_from_charge(db, data: dict) -> tuple[ServiceCheckout, ServiceSubscription]:
    transaction_id = str(data.get("id") or "").strip()
    if not transaction_id:
        raise HTTPException(400, "Tap charge id is missing")

    checkout = _checkout_for_transaction(
        db,
        transaction_id,
        provider="tap",
    )
    if checkout is not None:
        subscription = db.get(ServiceSubscription, checkout.service_subscription_id)
        if subscription is None:
            raise HTTPException(409, "Xvond subscription for Tap payment no longer exists")
        return checkout, subscription

    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    try:
        company_id = int(metadata.get("xvond_company_id"))
        subscription_id = int(metadata.get("xvond_service_subscription_id"))
        plan_id = int(metadata.get("xvond_plan_id"))
    except (TypeError, ValueError):
        raise HTTPException(409, "Tap charge is not linked to an Xvond checkout")

    subscription = db.get(ServiceSubscription, subscription_id)
    plan = db.get(ServicePlan, plan_id)
    if (
        subscription is None
        or plan is None
        or subscription.company_id != company_id
        or subscription.plan_id != plan_id
    ):
        raise HTTPException(409, "Tap charge does not match the Xvond subscription")

    transaction = data.get("transaction")
    transaction = transaction if isinstance(transaction, dict) else {}
    checkout = ServiceCheckout(
        company_id=company_id,
        service_subscription_id=subscription.id,
        plan_id=plan.id,
        provider="tap",
        provider_transaction_id=transaction_id,
        provider_subscription_id=None,
        status="pending",
        checkout_url=str(transaction.get("url") or "").strip() or None,
        amount=plan.monthly_price,
        currency=plan.currency,
    )
    db.add(checkout)
    db.flush()
    return checkout, subscription


def _upsert_tap_payment_profile(
    db,
    *,
    subscription: ServiceSubscription,
    data: dict,
) -> ServicePaymentProfile | None:
    agreement = data.get("payment_agreement")
    agreement = agreement if isinstance(agreement, dict) else {}
    contract = agreement.get("contract")
    contract = contract if isinstance(contract, dict) else {}
    customer = data.get("customer")
    customer = customer if isinstance(customer, dict) else {}
    card = data.get("card")
    card = card if isinstance(card, dict) else {}

    customer_id = str(
        customer.get("id")
        or contract.get("customer_id")
        or ""
    ).strip()
    card_id = str(card.get("id") or "").strip()
    agreement_id = str(agreement.get("id") or "").strip()

    if not customer_id or not card_id or not agreement_id:
        return None

    item = (
        db.query(ServicePaymentProfile)
        .filter(
            ServicePaymentProfile.service_subscription_id == subscription.id,
            ServicePaymentProfile.provider == "tap",
        )
        .with_for_update()
        .first()
    )
    config = protect_config(
        {
            "provider_customer_token": customer_id,
            "provider_card_token": card_id,
            "payment_agreement_token": agreement_id,
            "card_brand": str(card.get("brand") or "").strip() or None,
            "card_last4": str(
                card.get("last_four")
                or card.get("last4")
                or ""
            ).strip() or None,
        }
    )
    if item is None:
        item = ServicePaymentProfile(
            company_id=subscription.company_id,
            service_subscription_id=subscription.id,
            provider="tap",
            status="active",
            provider_config=config,
        )
        db.add(item)
    else:
        item.company_id = subscription.company_id
        item.status = "active"
        item.provider_config = config
    db.flush()
    return item


def _sync_tap_renewal_attempt(
    db,
    *,
    transaction_id: str,
    charge_status: str,
    renewal_key: str | None = None,
) -> ServiceRenewalAttempt | None:
    query = db.query(ServiceRenewalAttempt).filter(
        ServiceRenewalAttempt.provider == "tap"
    )
    attempt = (
        query.filter(
            ServiceRenewalAttempt.provider_transaction_id == transaction_id,
        )
        .order_by(ServiceRenewalAttempt.id.desc())
        .with_for_update()
        .first()
    )
    if attempt is None and str(renewal_key or "").strip():
        attempt = (
            query.filter(
                ServiceRenewalAttempt.idempotency_key == str(renewal_key).strip(),
            )
            .order_by(ServiceRenewalAttempt.id.desc())
            .with_for_update()
            .first()
        )
    if attempt is None:
        return None

    if transaction_id and not attempt.provider_transaction_id:
        attempt.provider_transaction_id = transaction_id

    if charge_status == "CAPTURED":
        attempt.status = "captured"
        attempt.last_error_code = None
    elif charge_status == "UNKNOWN":
        attempt.status = "unknown"
        attempt.last_error_code = "tap_charge_unknown"
    elif charge_status in {
        "ABANDONED",
        "CANCELLED",
        "FAILED",
        "DECLINED",
        "RESTRICTED",
        "VOID",
        "TIMEDOUT",
    }:
        attempt.status = "failed"
        attempt.last_error_code = f"tap_{charge_status.lower()}"
    else:
        attempt.status = "submitted"
    return attempt


def _process_tap_charge(db, data: dict) -> dict:
    checkout, subscription = _tap_checkout_from_charge(db, data)
    status = str(data.get("status") or "").strip().upper()
    transaction_id = str(data.get("id") or "").strip()
    metadata = data.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    recurring_charge = str(metadata.get("xvond_renewal") or "").strip().lower() == "true"
    now = _utcnow_naive()

    renewal_attempt = _sync_tap_renewal_attempt(
        db,
        transaction_id=transaction_id,
        charge_status=status,
        renewal_key=str(metadata.get("xvond_renewal_key") or "").strip() or None,
    )

    if status == "CAPTURED":
        checkout.status = "completed"
        if recurring_charge:
            start = max(subscription.current_period_end, now)
        else:
            start = now
        subscription.status = "active"
        subscription.plan_id = checkout.plan_id
        subscription.current_period_start = start
        subscription.current_period_end = _add_month(start)
        profile = _upsert_tap_payment_profile(
            db,
            subscription=subscription,
            data=data,
        )
    elif status in {
        "ABANDONED",
        "CANCELLED",
        "FAILED",
        "DECLINED",
        "RESTRICTED",
        "VOID",
        "TIMEDOUT",
    }:
        checkout.status = "failed"
        profile = None
        if recurring_charge and subscription.current_period_end <= now:
            subscription.status = "past_due"
    elif status == "UNKNOWN":
        checkout.status = "unknown"
        profile = None
        if recurring_charge and subscription.current_period_end <= now:
            subscription.status = "past_due"
    else:
        checkout.status = "pending"
        profile = None

    return {
        "subscription_id": subscription.id,
        "company_id": subscription.company_id,
        "plan_id": subscription.plan_id,
        "status": subscription.status,
        "charge_status": status.lower(),
        "transaction_id": transaction_id,
        "checkout_id": checkout.id,
        "payment_profile_id": profile.id if profile is not None else None,
        "renewal_attempt_id": renewal_attempt.id if renewal_attempt is not None else None,
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


@router.post("/tap")
async def tap_billing_webhook(
    request: Request,
    hashstring: str | None = Header(default=None, alias="hashstring"),
):
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid Tap webhook body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid Tap webhook body")

    try:
        tap_gateway.verify_webhook(
            payload=payload,
            hashstring_header=hashstring,
        )
    except PaymentGatewayError as exc:
        raise HTTPException(401, "Invalid Tap webhook") from exc

    charge_id = str(payload.get("id") or "").strip()
    status = str(payload.get("status") or "").strip().upper()
    transaction = payload.get("transaction")
    transaction = transaction if isinstance(transaction, dict) else {}
    created = str(transaction.get("created") or payload.get("created") or "").strip()
    if not charge_id or not status:
        raise HTTPException(400, "Tap webhook identity is missing")

    event_type = f"charge.{status.lower()}"
    event_id = f"{charge_id}:{status}:{created}"[:160]

    db = SessionLocal()
    try:
        existing = (
            db.query(ServicePaymentEvent)
            .filter(
                ServicePaymentEvent.provider == "tap",
                ServicePaymentEvent.provider_event_id == event_id,
            )
            .first()
        )
        if existing is not None:
            return {"status": "duplicate", "event_id": event_id}

        result = _process_tap_charge(db, payload)
        checkout = db.get(ServiceCheckout, result["checkout_id"])

        db.add(
            ServicePaymentEvent(
                company_id=result["company_id"],
                service_checkout_id=checkout.id if checkout is not None else None,
                provider="tap",
                provider_event_id=event_id,
                event_type=event_type,
            )
        )
        audit_service.log(
            db=db,
            action="billing.payment_webhook_processed",
            resource_type="billing",
            resource_id=None,
            company_id=result["company_id"],
            details={
                "provider": "tap",
                "event_id": event_id,
                "event_type": event_type,
                "result_status": result.get("status"),
                "charge_status": result.get("charge_status"),
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
