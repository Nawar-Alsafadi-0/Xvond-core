from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func

from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_operator
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIUsage
from backend.app.modules.billing.service_models import ServiceSubscription
from backend.app.modules.channels.models import AgentChannel, ManagedChannelOutboundDelivery
from backend.app.modules.channels.whatsapp_models import WhatsAppOutboundDelivery
from backend.app.modules.tools.business_models import ActionRequest

router = APIRouter(prefix="/admin/dashboard", tags=["Xvond Admin - Dashboard"])
UNRESOLVED_EXTERNAL = {"executing", "external_failed", "cancelling"}
UNRESOLVED_DELIVERY = {"failed", "unknown"}
LIFECYCLE_ORDER = (
    "onboarding",
    "testing",
    "live",
    "paused",
    "suspended",
    "cancelled",
    "archived",
)


def _attention_items(db, day_ago: datetime) -> list[dict]:
    failed_rows = (
        db.query(AIUsage.company_id, func.count(AIUsage.id))
        .filter(
            AIUsage.status == "failed",
            AIUsage.created_at >= day_ago,
        )
        .group_by(AIUsage.company_id)
        .all()
    )
    unresolved_rows = (
        db.query(ActionRequest.company_id, func.count(ActionRequest.id))
        .filter(ActionRequest.status.in_(UNRESOLVED_EXTERNAL))
        .group_by(ActionRequest.company_id)
        .all()
    )
    delivery_rows = (
        db.query(
            WhatsAppOutboundDelivery.company_id,
            func.count(WhatsAppOutboundDelivery.id),
        )
        .filter(WhatsAppOutboundDelivery.status.in_(UNRESOLVED_DELIVERY))
        .group_by(WhatsAppOutboundDelivery.company_id)
        .all()
    )
    managed_delivery_rows = (
        db.query(
            ManagedChannelOutboundDelivery.company_id,
            func.count(ManagedChannelOutboundDelivery.id),
        )
        .filter(ManagedChannelOutboundDelivery.status.in_(UNRESOLVED_DELIVERY))
        .group_by(ManagedChannelOutboundDelivery.company_id)
        .all()
    )
    company_ids = {
        int(company_id)
        for company_id, _count in [*failed_rows, *unresolved_rows, *delivery_rows, *managed_delivery_rows]
        if company_id is not None
    }
    names = {}
    if company_ids:
        names = {
            company.id: company.name
            for company in db.query(Company).filter(Company.id.in_(company_ids)).all()
        }

    items = []
    for company_id, count in unresolved_rows:
        items.append(
            {
                "type": "external_operation",
                "severity": "critical",
                "company_id": int(company_id),
                "company_name": names.get(company_id, f"Company #{company_id}"),
                "count": int(count or 0),
                "tab": "overview",
                "title": "External operations need reconciliation",
            }
        )
    for company_id, count in delivery_rows:
        items.append(
            {
                "type": "whatsapp_delivery",
                "severity": "critical",
                "company_id": int(company_id),
                "company_name": names.get(company_id, f"Company #{company_id}"),
                "count": int(count or 0),
                "tab": "overview",
                "title": "WhatsApp deliveries need review",
            }
        )
    for company_id, count in managed_delivery_rows:
        items.append(
            {
                "type": "managed_channel_delivery",
                "severity": "critical",
                "company_id": int(company_id),
                "company_name": names.get(company_id, f"Company #{company_id}"),
                "count": int(count or 0),
                "tab": "overview",
                "title": "Managed channel deliveries need review",
            }
        )
    for company_id, count in failed_rows:
        items.append(
            {
                "type": "ai_failure",
                "severity": "warning",
                "company_id": int(company_id),
                "company_name": names.get(company_id, f"Company #{company_id}"),
                "count": int(count or 0),
                "tab": "usage",
                "title": "AI requests failed in the last 24 hours",
            }
        )
    items.sort(
        key=lambda item: (
            0 if item["severity"] == "critical" else 1,
            -item["count"],
            item["company_id"],
        )
    )
    return items[:20]


def _lifecycle_counts(db) -> dict[str, int]:
    counts = {status: 0 for status in LIFECYCLE_ORDER}
    rows = (
        db.query(Company.lifecycle_status, func.count(Company.id))
        .group_by(Company.lifecycle_status)
        .all()
    )
    for status, count in rows:
        key = str(status or "onboarding").strip().lower()
        counts[key] = int(count or 0)
    return counts


@router.get("/summary")
def summary(current_admin: User = Depends(require_xvond_operator)):
    db = SessionLocal()
    try:
        now = datetime.now(UTC).replace(tzinfo=None)
        day_ago = now - timedelta(hours=24)
        month_ago = now - timedelta(days=30)

        companies = db.query(func.count(Company.id)).scalar() or 0
        lifecycle_counts = _lifecycle_counts(db)
        source_counts = {"managed": 0, "self_service": 0}
        for source, count in (
            db.query(Company.onboarding_source, func.count(Company.id))
            .group_by(Company.onboarding_source)
            .all()
        ):
            key = str(source or "managed").strip().lower()
            if key in source_counts:
                source_counts[key] = int(count or 0)
        active_companies = (
            db.query(func.count(Company.id))
            .filter(Company.active.is_(True))
            .scalar()
            or 0
        )
        agents = db.query(func.count(AIAgent.id)).scalar() or 0
        active_agents = (
            db.query(func.count(AIAgent.id))
            .filter(AIAgent.enabled.is_(True))
            .scalar()
            or 0
        )
        failed_ai_24h = (
            db.query(func.count(AIUsage.id))
            .filter(
                AIUsage.status == "failed",
                AIUsage.created_at >= day_ago,
            )
            .scalar()
            or 0
        )
        unresolved_external = (
            db.query(func.count(ActionRequest.id))
            .filter(ActionRequest.status.in_(UNRESOLVED_EXTERNAL))
            .scalar()
            or 0
        )
        unresolved_deliveries = (
            db.query(func.count(WhatsAppOutboundDelivery.id))
            .filter(WhatsAppOutboundDelivery.status.in_(UNRESOLVED_DELIVERY))
            .scalar()
            or 0
        )
        unresolved_managed_deliveries = (
            db.query(func.count(ManagedChannelOutboundDelivery.id))
            .filter(ManagedChannelOutboundDelivery.status.in_(UNRESOLVED_DELIVERY))
            .scalar()
            or 0
        )
        active_channels = (
            db.query(func.count(AgentChannel.id))
            .filter(AgentChannel.enabled.is_(True))
            .scalar()
            or 0
        )
        ai_requests_24h = (
            db.query(func.count(AIUsage.id))
            .filter(AIUsage.created_at >= day_ago)
            .scalar()
            or 0
        )
        provider_cost_30d = (
            db.query(func.coalesce(func.sum(AIUsage.provider_cost), 0))
            .filter(AIUsage.created_at >= month_ago)
            .scalar()
            or 0
        )

        return {
            "companies": companies,
            "lifecycle_counts": lifecycle_counts,
            "source_counts": source_counts,
            "active_companies": active_companies,
            "inactive_companies": max(0, companies - active_companies),
            "users": db.query(func.count(User.id)).scalar() or 0,
            "agents": agents,
            "active_agents": active_agents,
            "inactive_agents": max(0, agents - active_agents),
            "active_channels": active_channels,
            "conversations": db.query(func.count(AIConversation.id)).scalar() or 0,
            "ai_requests": db.query(func.count(AIUsage.id)).scalar() or 0,
            "ai_requests_24h": ai_requests_24h,
            "failed_ai_requests_24h": failed_ai_24h,
            "unresolved_external_operations": unresolved_external,
            "unresolved_whatsapp_deliveries": unresolved_deliveries,
            "unresolved_managed_channel_deliveries": unresolved_managed_deliveries,
            "total_tokens": db.query(
                func.coalesce(func.sum(AIUsage.total_tokens), 0)
            ).scalar() or 0,
            "provider_cost": db.query(
                func.coalesce(func.sum(AIUsage.provider_cost), 0)
            ).scalar() or 0,
            "provider_cost_30d": provider_cost_30d,
            "active_subscriptions": db.query(func.count(ServiceSubscription.id)).filter(
                ServiceSubscription.status == "active",
                ServiceSubscription.current_period_start <= now,
                ServiceSubscription.current_period_end > now,
            ).scalar() or 0,
            "attention": {
                "inactive_companies": max(0, companies - active_companies),
                "inactive_agents": max(0, agents - active_agents),
                "failed_ai_requests_24h": failed_ai_24h,
                "unresolved_external_operations": unresolved_external,
                "unresolved_whatsapp_deliveries": unresolved_deliveries,
                "unresolved_managed_channel_deliveries": unresolved_managed_deliveries,
            },
            "attention_items": _attention_items(db, day_ago),
        }
    finally:
        db.close()
