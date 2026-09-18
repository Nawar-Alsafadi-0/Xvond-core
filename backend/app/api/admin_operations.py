from datetime import UTC, datetime
import os
from pathlib import Path
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import func

from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_admin, require_xvond_operator
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIUsage
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.channels.acceptance import mark_customer_roundtrip
from backend.app.modules.channels.catalog import canonical_channel_type
from backend.app.modules.channels.managed_delivery import attempt_delivery as attempt_managed_delivery
from backend.app.modules.channels.models import AgentChannel, ManagedChannelOutboundDelivery
from backend.app.modules.channels.whatsapp_delivery import attempt_delivery
from backend.app.modules.channels.whatsapp_models import WhatsAppOutboundDelivery
from backend.app.modules.channels.whatsapp_queue import whatsapp_job_queue
from backend.app.modules.solutions.catalog import SERVICE_CATALOG
from backend.app.modules.tools.business_models import ActionRequest

router = APIRouter(prefix="/admin/operations", tags=["Xvond Admin - Operations"])
UNRESOLVED_EXTERNAL = {"executing", "external_failed", "cancelling"}
UNRESOLVED_DELIVERY = {"failed", "unknown"}
RECONCILIATION_OUTCOMES = {"executed", "not_executed", "cancelled"}


class ReconcileExternalOperation(BaseModel):
    outcome: str
    note: str | None = None


class ReconcileManagedChannelDelivery(BaseModel):
    outcome: str
    provider_message_id: str | None = None
    note: str | None = None


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _backup_marker(name: str, *, expected: bool, stale_after: int) -> dict:
    status_dir = Path(os.getenv("BACKUP_STATUS_DIR", "/backup-status"))
    path = status_dir / name
    now = int(time.time())
    if not expected:
        return {
            "status": "not_configured",
            "expected": False,
            "last_success_at": None,
            "age_seconds": None,
            "stale_after_seconds": stale_after,
        }
    try:
        epoch = int(path.read_text(encoding="utf-8").strip())
        if epoch <= 0 or epoch > now + 300:
            raise ValueError("invalid backup marker")
    except (OSError, ValueError):
        return {
            "status": "missing",
            "expected": True,
            "last_success_at": None,
            "age_seconds": None,
            "stale_after_seconds": stale_after,
        }
    age = max(0, now - epoch)
    return {
        "status": "healthy" if age <= stale_after else "stale",
        "expected": True,
        "last_success_at": datetime.fromtimestamp(epoch, UTC),
        "age_seconds": age,
        "stale_after_seconds": stale_after,
    }


def get_company_or_404(db, company_id: int):
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _operation_metadata(item: ActionRequest) -> dict:
    """Return operator-safe metadata without tenant customer content."""
    return {
        "id": item.id,
        "company_id": item.company_id,
        "agent_id": item.agent_id,
        "action_type": item.action_type,
        "status": item.status,
        "created_at": item.created_at,
    }


def _delivery_metadata(item: WhatsAppOutboundDelivery) -> dict:
    """Operator-safe transport metadata; never expose message/contact payloads."""
    return {
        "id": item.id,
        "company_id": item.company_id,
        "agent_id": item.agent_id,
        "conversation_id": item.conversation_id,
        "channel_id": item.channel_id,
        "status": item.status,
        "retryable": bool(item.retryable),
        "attempts": int(item.attempts or 0),
        "provider_message_id": item.provider_message_id,
        "last_status_code": item.last_status_code,
        "last_error_code": item.last_error_code,
        "accepted_at": item.accepted_at,
        "delivered_at": item.delivered_at,
        "read_at": item.read_at,
        "failed_at": item.failed_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _managed_delivery_metadata(item: ManagedChannelOutboundDelivery) -> dict:
    """Operator-safe managed transport metadata without customer content."""
    return {
        "id": item.id,
        "company_id": item.company_id,
        "agent_id": item.agent_id,
        "conversation_id": item.conversation_id,
        "channel_id": item.channel_id,
        "status": item.status,
        "retryable": bool(item.retryable),
        "attempts": int(item.attempts or 0),
        "provider_message_id": item.provider_message_id,
        "last_error_code": item.last_error_code,
        "accepted_at": item.accepted_at,
        "failed_at": item.failed_at,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


@router.get("/backups/status")
def backup_status(current_admin: User = Depends(require_xvond_operator)):
    """Backup freshness only; repository locations and credentials stay private."""
    try:
        stale_after = int(os.getenv("BACKUP_STALE_AFTER_SECONDS", "129600"))
    except ValueError:
        stale_after = 129600
    stale_after = max(3600, min(stale_after, 604800))
    offsite_expected = bool(
        str(os.getenv("RESTIC_REPOSITORY") or "").strip()
        and str(os.getenv("RESTIC_PASSWORD") or "").strip()
    )
    local = _backup_marker(
        "local_success_epoch",
        expected=True,
        stale_after=stale_after,
    )
    offsite = _backup_marker(
        "offsite_success_epoch",
        expected=offsite_expected,
        stale_after=stale_after,
    )
    overall = "healthy"
    if local["status"] != "healthy" or (
        offsite_expected and offsite["status"] != "healthy"
    ):
        overall = "attention_required"
    return {
        "status": overall,
        "local": local,
        "offsite": offsite,
    }


@router.get("/companies/{company_id}/usage")
def company_usage(company_id: int, current_admin: User = Depends(require_xvond_operator)):
    """Operational usage/cost telemetry only; no prompts or conversation content."""
    db = SessionLocal()
    try:
        get_company_or_404(db, company_id)
        summary = db.query(
            func.count(AIUsage.id),
            func.coalesce(func.sum(AIUsage.input_tokens), 0),
            func.coalesce(func.sum(AIUsage.output_tokens), 0),
            func.coalesce(func.sum(AIUsage.total_tokens), 0),
            func.coalesce(func.sum(AIUsage.provider_cost), 0),
        ).filter(AIUsage.company_id == company_id).first()
        items = db.query(AIUsage).filter(
            AIUsage.company_id == company_id
        ).order_by(AIUsage.id.desc()).limit(500).all()
        return {
            "company_id": company_id,
            "summary": {
                "requests": summary[0],
                "input_tokens": summary[1],
                "output_tokens": summary[2],
                "total_tokens": summary[3],
                "provider_cost": summary[4],
            },
            "usage": [
                {
                    "id": item.id,
                    "agent_id": item.agent_id,
                    "provider": item.provider,
                    "model": item.model,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "total_tokens": item.total_tokens,
                    "provider_cost": item.provider_cost,
                    "status": item.status,
                    "latency_ms": item.latency_ms,
                    "created_at": item.created_at,
                }
                for item in items
            ],
        }
    finally:
        db.close()


@router.get("/subscriptions")
def subscriptions(current_admin: User = Depends(require_xvond_operator)):
    """Canonical service subscriptions across all companies."""
    db = SessionLocal()
    try:
        rows = (
            db.query(ServiceSubscription, Company, ServicePlan)
            .join(Company, Company.id == ServiceSubscription.company_id)
            .join(ServicePlan, ServicePlan.id == ServiceSubscription.plan_id)
            .order_by(ServiceSubscription.id.desc())
            .all()
        )
        return {
            "subscriptions": [
                {
                    "id": subscription.id,
                    "company_id": company.id,
                    "company_name": company.name,
                    "service_code": subscription.service_code,
                    "service_name": SERVICE_CATALOG.get(subscription.service_code, {}).get(
                        "name", subscription.service_code
                    ),
                    "plan_id": plan.id,
                    "plan_name": plan.name,
                    "tier": plan.tier,
                    "monthly_price": plan.monthly_price,
                    "currency": plan.currency,
                    "status": subscription.status,
                    "current_period_start": subscription.current_period_start,
                    "current_period_end": subscription.current_period_end,
                }
                for subscription, company, plan in rows
            ]
        }
    finally:
        db.close()


@router.get("/companies/{company_id}/external-unresolved")
def unresolved_external_operations(
    company_id: int,
    current_admin: User = Depends(require_xvond_operator),
):
    """Expose only technical operation metadata needed for reconciliation."""
    db = SessionLocal()
    try:
        get_company_or_404(db, company_id)
        items = db.query(ActionRequest).filter(
            ActionRequest.company_id == company_id,
            ActionRequest.status.in_(UNRESOLVED_EXTERNAL),
        ).order_by(ActionRequest.id.desc()).all()
        return {
            "count": len(items),
            "requests": [_operation_metadata(item) for item in items],
        }
    finally:
        db.close()


@router.get("/whatsapp/deliveries/unresolved")
def unresolved_whatsapp_deliveries(
    company_id: int | None = None,
    limit: int = 100,
    current_admin: User = Depends(require_xvond_operator),
):
    """List unresolved transport state without tenant message/contact content."""
    db = SessionLocal()
    try:
        safe_limit = max(1, min(int(limit or 100), 500))
        query = db.query(WhatsAppOutboundDelivery).filter(
            WhatsAppOutboundDelivery.status.in_(UNRESOLVED_DELIVERY)
        )
        if company_id is not None:
            get_company_or_404(db, company_id)
            query = query.filter(WhatsAppOutboundDelivery.company_id == company_id)
        rows = query.order_by(WhatsAppOutboundDelivery.id.desc()).limit(safe_limit).all()
        return {
            "count": len(rows),
            "deliveries": [_delivery_metadata(row) for row in rows],
        }
    finally:
        db.close()


@router.post("/whatsapp/deliveries/{delivery_id}/retry")
def retry_whatsapp_delivery(
    delivery_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    """Retry only a provider-confirmed failed/retryable delivery.

    ``unknown`` means Xvond cannot prove whether Meta accepted the prior send, so
    automatically retrying it could duplicate a customer-facing message.
    """
    db = SessionLocal()
    try:
        row = (
            db.query(WhatsAppOutboundDelivery)
            .filter(WhatsAppOutboundDelivery.id == delivery_id)
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(404, "WhatsApp delivery not found")
        if row.status == "unknown":
            raise HTTPException(
                409,
                "Delivery outcome is unknown and must be reconciled before any resend",
            )
        if row.status != "failed" or not row.retryable:
            raise HTTPException(409, "WhatsApp delivery is not safely retryable")

        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.id == row.channel_id,
                AgentChannel.company_id == row.company_id,
                AgentChannel.agent_id == row.agent_id,
                AgentChannel.channel_type == "whatsapp",
                AgentChannel.enabled.is_(True),
            )
            .first()
        )
        if channel is None:
            raise HTTPException(409, "WhatsApp channel is not active")
        config = reveal_config(channel.config) or {}
        audit_service.log(
            db=db,
            company_id=row.company_id,
            action="whatsapp.delivery_retry_requested",
            resource_type="whatsapp_delivery",
            resource_id=row.id,
            user_id=current_admin.id,
            details={
                "conversation_id": row.conversation_id,
                "channel_id": row.channel_id,
                "attempts_before": int(row.attempts or 0),
                "previous_error_code": row.last_error_code,
            },
        )
        db.commit()
        result = attempt_delivery(db, delivery_id=row.id, config=config)
        refreshed = db.get(WhatsAppOutboundDelivery, row.id)
        audit_service.log(
            db=db,
            company_id=row.company_id,
            action="whatsapp.delivery_retry_completed",
            resource_type="whatsapp_delivery",
            resource_id=row.id,
            user_id=current_admin.id,
            details={
                "conversation_id": row.conversation_id,
                "channel_id": row.channel_id,
                "status": refreshed.status if refreshed is not None else result.get("status"),
                "attempts": int(refreshed.attempts or 0) if refreshed is not None else result.get("attempts"),
            },
        )
        db.commit()
        return {
            "status": "retried",
            "delivery": _delivery_metadata(refreshed),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/managed-channels/deliveries/unresolved")
def unresolved_managed_channel_deliveries(
    company_id: int | None = None,
    limit: int = 100,
    current_admin: User = Depends(require_xvond_operator),
):
    db = SessionLocal()
    try:
        safe_limit = max(1, min(int(limit or 100), 500))
        query = db.query(ManagedChannelOutboundDelivery).filter(
            ManagedChannelOutboundDelivery.status.in_(UNRESOLVED_DELIVERY)
        )
        if company_id is not None:
            get_company_or_404(db, company_id)
            query = query.filter(
                ManagedChannelOutboundDelivery.company_id == company_id
            )
        rows = (
            query.order_by(ManagedChannelOutboundDelivery.id.desc())
            .limit(safe_limit)
            .all()
        )
        return {
            "count": len(rows),
            "deliveries": [_managed_delivery_metadata(row) for row in rows],
        }
    finally:
        db.close()


@router.post("/managed-channels/deliveries/{delivery_id}/retry")
def retry_managed_channel_delivery(
    delivery_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        row = (
            db.query(ManagedChannelOutboundDelivery)
            .filter(ManagedChannelOutboundDelivery.id == delivery_id)
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Managed channel delivery not found")
        if row.status == "unknown":
            raise HTTPException(
                409,
                "Delivery outcome is unknown and must be reconciled before any resend",
            )
        if row.status != "failed" or not row.retryable:
            raise HTTPException(409, "Managed channel delivery is not safely retryable")

        audit_service.log(
            db=db,
            company_id=row.company_id,
            action="managed_channel.delivery_retry_requested",
            resource_type="managed_channel_delivery",
            resource_id=row.id,
            user_id=current_admin.id,
            details={
                "conversation_id": row.conversation_id,
                "channel_id": row.channel_id,
                "attempts_before": int(row.attempts or 0),
                "previous_error_code": row.last_error_code,
            },
        )
        db.commit()

        result = attempt_managed_delivery(db, delivery_id=row.id)
        refreshed = db.get(ManagedChannelOutboundDelivery, row.id)
        audit_service.log(
            db=db,
            company_id=row.company_id,
            action="managed_channel.delivery_retry_completed",
            resource_type="managed_channel_delivery",
            resource_id=row.id,
            user_id=current_admin.id,
            details={
                "conversation_id": row.conversation_id,
                "channel_id": row.channel_id,
                "status": (
                    refreshed.status
                    if refreshed is not None
                    else result.get("status")
                ),
                "attempts": (
                    int(refreshed.attempts or 0)
                    if refreshed is not None
                    else result.get("attempts")
                ),
            },
        )
        db.commit()
        return {
            "status": "retried",
            "delivery": _managed_delivery_metadata(refreshed),
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/managed-channels/deliveries/{delivery_id}/reconcile")
def reconcile_managed_channel_delivery(
    delivery_id: int,
    data: ReconcileManagedChannelDelivery,
    current_admin: User = Depends(require_xvond_admin),
):
    outcome = str(data.outcome or "").strip().lower()
    if outcome not in {"sent", "not_sent"}:
        raise HTTPException(400, "Invalid reconciliation outcome")

    db = SessionLocal()
    try:
        row = (
            db.query(ManagedChannelOutboundDelivery)
            .filter(ManagedChannelOutboundDelivery.id == delivery_id)
            .with_for_update()
            .first()
        )
        if row is None:
            raise HTTPException(404, "Managed channel delivery not found")
        if row.status != "unknown":
            raise HTTPException(409, "Only unknown deliveries require reconciliation")

        now = _utcnow_naive()
        provider_message_id = str(data.provider_message_id or "").strip()
        if outcome == "sent":
            if not provider_message_id:
                raise HTTPException(
                    400,
                    "Provider message id is required when confirming a sent delivery",
                )
            row.status = "accepted"
            row.retryable = False
            row.provider_message_id = provider_message_id[:255]
            row.last_error_code = None
            row.accepted_at = now

            channel = (
                db.query(AgentChannel)
                .filter(
                    AgentChannel.id == row.channel_id,
                    AgentChannel.company_id == row.company_id,
                    AgentChannel.agent_id == row.agent_id,
                )
                .first()
            )
            if channel is not None:
                mark_customer_roundtrip(
                    channel,
                    source=(
                        f"{canonical_channel_type(channel.channel_type)}"
                        "_provider_reconciled"
                    ),
                )
        else:
            row.status = "failed"
            row.retryable = True
            row.last_error_code = "reconciled_not_sent"
            row.failed_at = now

        audit_service.log(
            db=db,
            company_id=row.company_id,
            action="managed_channel.delivery_reconciled",
            resource_type="managed_channel_delivery",
            resource_id=row.id,
            user_id=current_admin.id,
            details={
                "conversation_id": row.conversation_id,
                "channel_id": row.channel_id,
                "outcome": outcome,
                "provider_message_id": provider_message_id or None,
                "note": (data.note or "").strip()[:1000] or None,
            },
        )
        db.commit()
        db.refresh(row)
        return _managed_delivery_metadata(row)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.patch("/requests/{request_id}/reconcile")
def reconcile_external_operation(
    request_id: int,
    data: ReconcileExternalOperation,
    current_admin: User = Depends(require_xvond_admin),
):
    outcome = data.outcome.strip().lower()
    if outcome not in RECONCILIATION_OUTCOMES:
        raise HTTPException(400, "Invalid reconciliation outcome")
    db = SessionLocal()
    try:
        item = db.query(ActionRequest).filter(ActionRequest.id == request_id).first()
        if item is None:
            raise HTTPException(404, "Operation not found")
        if item.status not in UNRESOLVED_EXTERNAL:
            raise HTTPException(409, "Operation does not need external reconciliation")

        details = dict(item.details or {})
        previous = details.get("_xvond_execution")
        previous = dict(previous) if isinstance(previous, dict) else {}
        now = _utcnow_naive().isoformat()
        reconciliation = {
            "outcome": outcome,
            "note": (data.note or "").strip()[:1000] or None,
            "reconciled_at": now,
            "previous_state": previous,
        }
        details["_xvond_reconciliation"] = reconciliation

        if outcome == "executed":
            details["_xvond_execution"] = {
                **previous,
                "state": "confirmed",
                "operation": "execute",
                "updated_at": now,
                "reconciled": True,
            }
            item.status = "confirmed"
        elif outcome == "cancelled":
            details["_xvond_execution"] = {
                **previous,
                "state": "confirmed",
                "operation": "cancel",
                "updated_at": now,
                "reconciled": True,
            }
            item.status = "cancelled"
        else:
            details["_xvond_execution"] = {
                **previous,
                "state": "reconciled_not_executed",
                "updated_at": now,
                "reconciled": True,
            }
            item.status = "new"

        item.details = details
        db.commit()
        db.refresh(item)
        return _operation_metadata(item)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/workers/whatsapp")
def whatsapp_worker_status(current_admin: User = Depends(require_xvond_operator)):
    try:
        return whatsapp_job_queue.stats()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="WhatsApp worker queue unavailable") from exc


@router.get("/workers/whatsapp/dead")
def whatsapp_dead_jobs(
    limit: int = 50,
    current_admin: User = Depends(require_xvond_operator),
):
    try:
        return {"jobs": whatsapp_job_queue.dead_jobs(limit=limit)}
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="WhatsApp worker queue unavailable") from exc


@router.post("/workers/whatsapp/dead/retry")
def retry_whatsapp_dead_jobs(
    limit: int = 100,
    current_admin: User = Depends(require_xvond_admin),
):
    try:
        requeued = whatsapp_job_queue.requeue_dead(limit=limit)
        return {"status": "requeued", "requeued": requeued}
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="WhatsApp worker queue unavailable") from exc
