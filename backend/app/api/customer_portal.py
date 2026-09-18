from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends
from sqlalchemy import func

from backend.app.core.config_secrets import configured_secret_fields, public_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_user
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent, AIConversation, AIUsage
from backend.app.modules.billing.service_limits import service_limits
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.customer_ops.models import NotificationEvent
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.knowledge.models import KnowledgeDocument
from backend.app.modules.solutions.catalog import SERVICE_CATALOG
from backend.app.modules.solutions.portal import (
    BUSINESS_CAPABILITY_MODULES,
    build_customer_portal_navigation,
)
from backend.app.modules.tools.business_models import ActionRequest, HumanHandoff

router = APIRouter(prefix="/customer", tags=["Customer Portal"])

MANAGER_ROLES = {"owner", "admin", "manager"}
LIVE_CUSTOMER_CHANNELS = ("whatsapp", "website", "voice", "instagram")
OPEN_OPERATION_STATES = {
    "pending",
    "awaiting_confirmation",
    "in_progress",
    "processing",
    "executing",
    "external_failed",
    "cancelling",
    "pending_human",
}
ACTIVE_HANDOFF_STATES = {"pending", "in_progress"}


def _plain_limit(value):
    if value in (None, 0, "0"):
        return 0
    try:
        text = format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError, TypeError):
        return value
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _plan_limits(plan: ServicePlan) -> dict:
    return {key: _plain_limit(value) for key, value in (plan.limits or {}).items()}


def _service_data(db, subscription: ServiceSubscription, plan: ServicePlan) -> dict:
    usage = {}
    for metric, limit in (plan.limits or {}).items():
        usage[metric] = {
            "used": service_limits.used(db, subscription, metric),
            "limit": _plain_limit(limit),
        }
    now = datetime.utcnow()
    effective_status = subscription.status
    if effective_status == "active" and not (
        subscription.current_period_start <= now < subscription.current_period_end
    ):
        effective_status = "expired"
    return {
        "id": subscription.id,
        "service_code": subscription.service_code,
        "service_name": SERVICE_CATALOG.get(subscription.service_code, {}).get(
            "name", subscription.service_code
        ),
        "status": effective_status,
        "current_period_start": subscription.current_period_start,
        "current_period_end": subscription.current_period_end,
        "plan": {
            "id": plan.id,
            "name": plan.name,
            "tier": plan.tier,
            "monthly_price": plan.monthly_price,
            "currency": plan.currency,
            "limits": _plan_limits(plan),
        },
        "usage": usage,
    }


def _limit_warning_count(services: list[dict]) -> int:
    warnings = 0
    for service in services:
        for row in (service.get("usage") or {}).values():
            try:
                limit = Decimal(str(row.get("limit") or 0))
                used = Decimal(str(row.get("used") or 0))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if limit > 0 and used >= limit:
                warnings += 1
    return warnings


def _live_conversation_query(db, company_id: int):
    """Customer-facing operational counts must match the default live Inbox.

    Test Console and legacy/unclassified conversations remain available only via
    explicit diagnostic filters and must never inflate production dashboard data.
    """
    return db.query(AIConversation).filter(
        AIConversation.company_id == company_id,
        AIConversation.channel_type.in_(LIVE_CUSTOMER_CHANNELS),
    )


def _active_live_handoff_count(db, company_id: int) -> int:
    return int(
        db.query(func.count(HumanHandoff.id))
        .join(AIConversation, AIConversation.id == HumanHandoff.conversation_id)
        .filter(
            HumanHandoff.company_id == company_id,
            AIConversation.company_id == company_id,
            AIConversation.channel_type.in_(LIVE_CUSTOMER_CHANNELS),
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATES),
        )
        .scalar()
        or 0
    )


def _company_portal_state(company: Company) -> dict:
    return {
        "id": company.id,
        "name": company.name,
        "active": company.active,
        "lifecycle_status": company.lifecycle_status,
        "onboarding_source": company.onboarding_source,
        "lifecycle_updated_at": company.lifecycle_updated_at,
    }


def _staff_overview(db, current_user: User, company: Company) -> dict:
    agents = db.query(AIAgent).filter(AIAgent.company_id == company.id).all()
    channels = db.query(AgentChannel).filter(AgentChannel.company_id == company.id).all()
    conversation_count = _live_conversation_query(db, company.id).count()
    active_handoffs = _active_live_handoff_count(db, company.id)
    return {
        "company": _company_portal_state(company),
        "services": [],
        "subscription": None,
        "portal": {
            "access_level": "operator",
            "navigation": [
                {
                    "id": "dashboard",
                    "label": "Overview",
                    "loader": "dashboard",
                    "group": "Workspace",
                },
                {
                    "id": "conversations",
                    "label": "Customer Inbox",
                    "loader": "conversations",
                    "group": "Operations",
                },
            ],
            "active_services": [],
            "capabilities": [],
        },
        "billing": {},
        "summary": {
            "agents": len(agents),
            "active_agents": sum(1 for item in agents if item.enabled),
            "channels": len(channels),
            "active_channels": sum(1 for item in channels if item.enabled),
            "conversations": int(conversation_count),
            "active_handoffs": int(active_handoffs),
        },
        "channels": [],
        "integrations": [],
    }


@router.get("/overview")
def overview(current_user: User = Depends(require_customer_user)):
    db = SessionLocal()
    try:
        company_id = current_user.company_id
        company = db.query(Company).filter(Company.id == company_id).first()

        if current_user.role not in MANAGER_ROLES:
            return _staff_overview(db, current_user, company)

        service_rows = (
            db.query(ServiceSubscription, ServicePlan)
            .join(ServicePlan, ServicePlan.id == ServiceSubscription.plan_id)
            .filter(ServiceSubscription.company_id == company_id)
            .order_by(ServiceSubscription.service_code.asc())
            .all()
        )
        services = [_service_data(db, subscription, plan) for subscription, plan in service_rows]
        ai_service = next((x for x in services if x["service_code"] == "ai_agents"), None)

        enabled_module_rows = (
            db.query(CompanyModule)
            .filter(
                CompanyModule.company_id == company_id,
                CompanyModule.enabled.is_(True),
            )
            .all()
        )
        enabled_modules = {item.module_name for item in enabled_module_rows}
        active_service_codes = [
            item["service_code"] for item in services if item["status"] == "active"
        ]

        is_self_service_workspace = company.onboarding_source == "self_service"
        has_self_service_employee = bool(
            is_self_service_workspace
            and db.query(AIAgent.id).filter(AIAgent.company_id == company_id).first()
        )
        portal_service_codes = list(active_service_codes)
        if has_self_service_employee and "ai_agents" not in portal_service_codes:
            # A self-service draft may be managed in the portal before purchase.
            # This is display-only and does not create a paid entitlement or
            # enable runtime/channels.
            portal_service_codes.append("ai_agents")

        navigation = build_customer_portal_navigation(
            portal_service_codes,
            enabled_modules,
        )
        if is_self_service_workspace:
            navigation.insert(
                1,
                {
                    "id": "employee-builder",
                    "label": "Build your employee",
                    "loader": "employee-builder",
                    "group": "AI Workforce",
                    "service_code": "ai_agents",
                },
            )
        navigation.insert(
            max(len(navigation) - 1, 1),
            {
                "id": "users",
                "label": "Users",
                "loader": "users",
                "group": "Account",
            },
        )

        agents = db.query(AIAgent).filter(AIAgent.company_id == company_id).all()
        channels = db.query(AgentChannel).filter(AgentChannel.company_id == company_id).all()
        integrations = db.query(CompanyIntegration).filter(
            CompanyIntegration.company_id == company_id
        ).all()
        usage = db.query(
            func.count(AIUsage.id),
            func.coalesce(func.sum(AIUsage.total_tokens), 0),
        ).filter(AIUsage.company_id == company_id).first()
        day_ago = datetime.utcnow() - timedelta(hours=24)
        open_operations = (
            db.query(func.count(ActionRequest.id))
            .filter(
                ActionRequest.company_id == company_id,
                ActionRequest.status.in_(OPEN_OPERATION_STATES),
            )
            .scalar()
            or 0
        )
        active_handoffs = _active_live_handoff_count(db, company_id)
        unread_notifications = (
            db.query(func.count(NotificationEvent.id))
            .filter(
                NotificationEvent.company_id == company_id,
                NotificationEvent.read.is_(False),
            )
            .scalar()
            or 0
        )
        failed_ai_24h = (
            db.query(func.count(AIUsage.id))
            .filter(
                AIUsage.company_id == company_id,
                AIUsage.status == "failed",
                AIUsage.created_at >= day_ago,
            )
            .scalar()
            or 0
        )

        return {
            "company": _company_portal_state(company),
            "services": services,
            "subscription": ai_service,
            "portal": {
                "access_level": "manager",
                "navigation": navigation,
                "active_services": active_service_codes,
                "capabilities": sorted(
                    enabled_modules.intersection(BUSINESS_CAPABILITY_MODULES)
                ),
            },
            "billing": {
                "online_payments_enabled": False,
                "payment_provider": None,
                "payment_method": None,
            },
            "summary": {
                "agents": len(agents),
                "active_agents": sum(1 for item in agents if item.enabled),
                "conversations": _live_conversation_query(db, company_id).count(),
                "requests": int(usage[0] or 0),
                "tokens": int(usage[1] or 0),
                "knowledge_documents": db.query(KnowledgeDocument).filter(
                    KnowledgeDocument.company_id == company_id,
                    KnowledgeDocument.enabled.is_(True),
                ).count(),
                "channels": len(channels),
                "active_channels": sum(1 for item in channels if item.enabled),
                "integrations": len(integrations),
                "open_operations": int(open_operations),
                "active_handoffs": int(active_handoffs),
                "unread_notifications": int(unread_notifications),
                "failed_ai_requests_24h": int(failed_ai_24h),
                "service_limit_warnings": _limit_warning_count(services),
            },
            "channels": [
                {
                    "id": item.id,
                    "agent_id": item.agent_id,
                    "type": item.channel_type,
                    "enabled": item.enabled,
                    "config": public_config(item.config),
                    "configured_secret_fields": configured_secret_fields(item.config),
                }
                for item in channels
            ],
            "integrations": [
                {
                    "id": item.id,
                    "type": item.integration_type,
                    "name": item.name,
                    "enabled": item.enabled,
                    "config": public_config(item.config),
                    "configured_secret_fields": configured_secret_fields(item.config),
                }
                for item in integrations
            ],
        }
    finally:
        db.close()
