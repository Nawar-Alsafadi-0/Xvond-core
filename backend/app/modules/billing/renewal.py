from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.modules.billing.payment_gateway import (
    PaymentGatewayError,
    PaymentGatewayOutcomeUnknown,
    tap_gateway,
)
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentProfile,
    ServicePlan,
    ServiceRenewalAttempt,
    ServiceSubscription,
)


FINAL_ATTEMPT_STATES = {"captured", "submitted", "unknown", "failed"}


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _renewal_key(subscription: ServiceSubscription) -> str:
    stamp = subscription.current_period_end.strftime("%Y%m%dT%H%M%S")
    return f"xvond-renew-{subscription.id}-{stamp}"


def _existing_attempt(db, *, key: str) -> ServiceRenewalAttempt | None:
    return (
        db.query(ServiceRenewalAttempt)
        .filter(
            ServiceRenewalAttempt.provider == "tap",
            ServiceRenewalAttempt.idempotency_key == key,
        )
        .first()
    )


def _ensure_attempt(
    db,
    *,
    subscription: ServiceSubscription,
) -> ServiceRenewalAttempt:
    key = _renewal_key(subscription)
    existing = _existing_attempt(db, key=key)
    if existing is not None:
        return existing

    item = ServiceRenewalAttempt(
        company_id=subscription.company_id,
        service_subscription_id=subscription.id,
        provider="tap",
        idempotency_key=key,
        period_end=subscription.current_period_end,
        status="pending",
        attempts=0,
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        item = _existing_attempt(db, key=key)
        if item is None:
            raise
    return item


def _payment_profile(db, subscription_id: int) -> ServicePaymentProfile | None:
    return (
        db.query(ServicePaymentProfile)
        .filter(
            ServicePaymentProfile.service_subscription_id == subscription_id,
            ServicePaymentProfile.provider == "tap",
            ServicePaymentProfile.status == "active",
        )
        .first()
    )


def _checkout_for_transaction(db, transaction_id: str) -> ServiceCheckout | None:
    return (
        db.query(ServiceCheckout)
        .filter(
            ServiceCheckout.provider == "tap",
            ServiceCheckout.provider_transaction_id == transaction_id,
        )
        .first()
    )


def _process_subscription(
    db,
    *,
    subscription: ServiceSubscription,
    now: datetime,
) -> str:
    plan = db.get(ServicePlan, subscription.plan_id)
    if plan is None or not plan.enabled:
        return "blocked"
    if Decimal(str(plan.monthly_price or 0)) <= 0:
        return "blocked"

    profile = _payment_profile(db, subscription.id)
    if profile is None:
        return "blocked"

    config = reveal_config(profile.provider_config) or {}
    customer_id = str(config.get("provider_customer_token") or "").strip()
    card_id = str(config.get("provider_card_token") or "").strip()
    agreement_id = str(config.get("payment_agreement_token") or "").strip()
    if not customer_id or not card_id or not agreement_id:
        profile.status = "incomplete"
        db.commit()
        return "blocked"

    attempt = _ensure_attempt(db, subscription=subscription)
    db.commit()
    db.refresh(attempt)

    if attempt.status == "sending":
        attempt.status = "unknown"
        attempt.last_error_code = "interrupted_after_charge_started"
        db.commit()
        return "unknown"
    if attempt.status in FINAL_ATTEMPT_STATES:
        return attempt.status

    attempt.status = "sending"
    attempt.attempts = int(attempt.attempts or 0) + 1
    attempt.last_error_code = None
    db.commit()

    try:
        result = tap_gateway.create_recurring_charge(
            company_id=subscription.company_id,
            service_subscription_id=subscription.id,
            plan_id=plan.id,
            plan_tier=plan.tier,
            service_code=subscription.service_code,
            amount=Decimal(str(plan.monthly_price or 0)),
            currency=plan.currency,
            customer_id=customer_id,
            card_id=card_id,
            payment_agreement_id=agreement_id,
            idempotency_key=attempt.idempotency_key,
        )
    except PaymentGatewayOutcomeUnknown:
        attempt = db.get(ServiceRenewalAttempt, attempt.id)
        attempt.status = "unknown"
        attempt.last_error_code = "provider_outcome_unknown"
        db.commit()
        return "unknown"
    except PaymentGatewayError:
        attempt = db.get(ServiceRenewalAttempt, attempt.id)
        attempt.status = "failed"
        attempt.last_error_code = "provider_request_failed"
        db.commit()
        return "failed"

    transaction_id = str(result.get("transaction_id") or "").strip()
    if not transaction_id:
        attempt = db.get(ServiceRenewalAttempt, attempt.id)
        attempt.status = "unknown"
        attempt.last_error_code = "provider_transaction_id_missing"
        db.commit()
        return "unknown"

    attempt = db.get(ServiceRenewalAttempt, attempt.id)
    if not attempt.provider_transaction_id:
        attempt.provider_transaction_id = transaction_id
    provider_status = str(result.get("status") or "").strip().lower()
    if attempt.status != "captured":
        attempt.status = "submitted"
        attempt.last_error_code = (
            None
            if provider_status in {"captured", "initiated", "in_progress", "pending"}
            else f"tap_sync_{provider_status}"[:160]
            if provider_status
            else None
        )

    checkout = _checkout_for_transaction(db, transaction_id)
    if checkout is None:
        checkout = ServiceCheckout(
            company_id=subscription.company_id,
            service_subscription_id=subscription.id,
            plan_id=plan.id,
            provider="tap",
            provider_transaction_id=transaction_id,
            provider_subscription_id=None,
            status="pending",
            checkout_url=None,
            amount=plan.monthly_price,
            currency=plan.currency,
        )
        db.add(checkout)
    elif (
        checkout.company_id != subscription.company_id
        or checkout.service_subscription_id != subscription.id
    ):
        attempt.status = "unknown"
        attempt.last_error_code = "provider_transaction_scope_conflict"
        db.commit()
        return "unknown"

    # Entitlement is deliberately NOT extended here. Only the verified Tap
    # webhook is allowed to advance the subscription period and create signed
    # provider evidence.
    db.commit()
    return attempt.status


def run_due_service_renewals_once(*, now: datetime | None = None) -> dict:
    summary = {
        "enabled": False,
        "checked": 0,
        "submitted": 0,
        "captured": 0,
        "blocked": 0,
        "unknown": 0,
        "failed": 0,
    }
    if (
        settings.BILLING_PROVIDER != "tap"
        or not settings.TAP_RECURRING_ENABLED
        or not settings.TAP_SAVE_CARD_FOR_RECURRING
        or not tap_gateway.configured()
    ):
        return summary

    summary["enabled"] = True
    current = now or _utcnow_naive()
    cutoff = current + timedelta(hours=settings.TAP_RENEWAL_LEAD_HOURS)

    db = SessionLocal()
    try:
        subscriptions = (
            db.query(ServiceSubscription)
            .filter(
                ServiceSubscription.service_code == "ai_agents",
                ServiceSubscription.status == "active",
                ServiceSubscription.current_period_end <= cutoff,
            )
            .order_by(ServiceSubscription.current_period_end.asc(), ServiceSubscription.id.asc())
            .limit(500)
            .all()
        )
        for subscription in subscriptions:
            summary["checked"] += 1
            outcome = _process_subscription(
                db,
                subscription=subscription,
                now=current,
            )
            if outcome in summary:
                summary[outcome] += 1
            else:
                summary["failed"] += 1
        return summary
    finally:
        db.close()
