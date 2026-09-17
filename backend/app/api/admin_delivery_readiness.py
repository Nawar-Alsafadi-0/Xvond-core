from fastapi import APIRouter, Depends, HTTPException

from backend.app.api.admin_agent_actions import _enabled_business_modules, _readiness
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_xvond_admin
from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.core.readiness import _channel_customer_accepted
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.profile_models import AIAgentProfile
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.channels.catalog import validate_channel_config
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument
from backend.app.modules.tools.models import AgentToolAssignment


router = APIRouter(
    prefix="/admin/delivery-readiness",
    tags=["Xvond Admin - Delivery Readiness"],
)

LEGACY_BUSINESS_TOOL_NAMES = frozenset({"booking", "order", "lead"})


def _action_state(db, company_id: int, agent_id: int) -> dict:
    assignments = (
        db.query(AgentToolAssignment)
        .filter(
            AgentToolAssignment.agent_id == agent_id,
            AgentToolAssignment.enabled.is_(True),
        )
        .all()
    )
    by_name = {str(item.tool_name or "").strip(): item for item in assignments}
    assignment = by_name.get("action_request")
    legacy_business_tools = sorted(set(by_name) & LEGACY_BUSINESS_TOOL_NAMES)

    if assignment is None and not legacy_business_tools:
        return {
            "requested": False,
            "ready": True,
            "enabled_count": 0,
            "issues": [],
            "requires_workflow_engine": False,
            "required_integration_ids": [],
            "legacy_business_tools": [],
        }

    issues = []
    if legacy_business_tools:
        issues.append(
            "Legacy business tools are still enabled: "
            + ", ".join(legacy_business_tools)
            + ". Configure canonical Business Actions before Go Live"
        )

    enabled_actions = []
    required_integration_ids = set()
    if assignment is not None:
        config = reveal_config(assignment.config) or {}
        stored = config.get("actions") or {}
        enabled_modules = _enabled_business_modules(db, company_id)

        for key, value in stored.items():
            if not isinstance(value, dict) or not value.get("enabled", False):
                continue
            action = {"key": key, **value}
            enabled_actions.append(action)
            for issue in _readiness(action, enabled_modules):
                issues.append(f"{action.get('label') or key}: {issue}")
            destination = action.get("destination") or {}
            if destination.get("type") == "integration" and destination.get("integration_id"):
                required_integration_ids.add(int(destination["integration_id"]))

        if not enabled_actions:
            issues.append(
                "Business Actions are enabled, but no customer operation is enabled and runtime-ready"
            )

    requested = bool(assignment is not None or legacy_business_tools)
    return {
        "requested": requested,
        "ready": bool(not issues and (assignment is None or enabled_actions)),
        "enabled_count": len(enabled_actions),
        "issues": issues,
        "requires_workflow_engine": bool(enabled_actions),
        "required_integration_ids": sorted(required_integration_ids),
        "legacy_business_tools": legacy_business_tools,
    }


def _channel_state(db, company_id: int, agent_id: int) -> dict:
    rows = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == company_id,
            AgentChannel.agent_id == agent_id,
        )
        .all()
    )
    configured = []
    enabled = []
    live = []
    customer_ready = []
    acceptance_pending = []

    for row in rows:
        config = reveal_config(row.config) or {}
        try:
            validate_channel_config(row.channel_type, config)
        except ValueError:
            continue
        configured.append(row)

        connection = None
        if row.channel_type == "whatsapp":
            connection = whatsapp_connection_state(config, verify_remote=True)
            connected = bool(connection["connected"])
        else:
            connected = True

        accepted = _channel_customer_accepted(
            channel_type=row.channel_type,
            channel_config=config,
            connected=connected,
            connection=connection,
        )

        if not row.enabled:
            continue
        enabled.append(row)
        if connected:
            live.append(row)
        if accepted:
            customer_ready.append(row)
        else:
            acceptance_pending.append(row)

    fully_customer_ready = bool(customer_ready) and not acceptance_pending
    return {
        "configured_count": len(configured),
        "enabled_count": len(enabled),
        "live_count": len(live),
        "customer_ready_count": len(customer_ready),
        "acceptance_pending_count": len(acceptance_pending),
        "configured": bool(configured),
        "live": bool(live),
        "customer_ready": fully_customer_ready,
    }


def _assert_workflow_runtime_ready(company_id: int, agent_id: int) -> None:
    """Fail closed before enabling an employee that can execute business actions.

    Configuration values prove only that a workflow endpoint was configured. Go Live
    requires the canonical workflow itself to answer a real health action so a sold
    booking/order/CRM/POS path cannot be enabled against a dead or inactive engine.
    """

    try:
        result = n8n_gateway.execute(
            company_id=company_id,
            agent_id=agent_id,
            action="health_check",
            data={"source": "delivery_readiness_go_live"},
        )
    except N8NGatewayError as exc:
        raise HTTPException(
            409,
            detail={
                "message": "Workflow Engine health check failed",
                "blockers": [
                    "Workflow Engine is not reachable for enabled business actions"
                ],
            },
        ) from exc

    data = result.get("data") if isinstance(result, dict) else None
    healthy = bool(
        isinstance(result, dict)
        and result.get("success") is True
        and isinstance(data, dict)
        and str(data.get("status") or "").strip().lower() == "ok"
    )
    if not healthy:
        raise HTTPException(
            409,
            detail={
                "message": "Workflow Engine health check failed",
                "blockers": [
                    "Workflow Engine did not confirm the canonical action workflow"
                ],
            },
        )


def _delivery_state(db, company_id: int, agent_id: int) -> dict:
    agent = (
        db.query(AIAgent)
        .filter(AIAgent.id == agent_id, AIAgent.company_id == company_id)
        .first()
    )
    if agent is None:
        raise HTTPException(404, "AI employee not found")

    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        raise HTTPException(404, "Company not found")

    profile = (
        db.query(AIAgentProfile)
        .filter(
            AIAgentProfile.agent_id == agent_id,
            AIAgentProfile.company_id == company_id,
        )
        .first()
    )
    profile_ready = profile is not None and bool(str(agent.name or "").strip())

    knowledge_count = (
        db.query(AgentKnowledge)
        .join(KnowledgeDocument, KnowledgeDocument.id == AgentKnowledge.document_id)
        .filter(
            AgentKnowledge.agent_id == agent_id,
            AgentKnowledge.enabled.is_(True),
            KnowledgeDocument.company_id == company_id,
            KnowledgeDocument.enabled.is_(True),
        )
        .count()
    )
    knowledge_ready = knowledge_count > 0

    channels = _channel_state(db, company_id, agent_id)
    actions = _action_state(db, company_id, agent_id)

    integration_issues = []
    for integration_id in actions["required_integration_ids"]:
        integration = (
            db.query(CompanyIntegration)
            .filter(
                CompanyIntegration.id == integration_id,
                CompanyIntegration.company_id == company_id,
                CompanyIntegration.enabled.is_(True),
            )
            .first()
        )
        if integration is None or not (reveal_config(integration.config) or {}):
            integration_issues.append(
                f"Connected App #{integration_id} is missing or not configured"
            )

    workflow_required = actions["requires_workflow_engine"]
    workflow_ready = (
        not workflow_required
        or (
            bool(settings.N8N_ENABLED)
            and bool(str(settings.N8N_WEBHOOK_URL or "").strip())
            and bool(str(settings.N8N_SHARED_SECRET or "").strip())
        )
    )

    setup_blockers = []
    employee_config = db.query(AgentConfig).filter(AgentConfig.agent_id == agent_id).first()
    if employee_config is not None and employee_config.agent_type == "employee":
        builder = (employee_config.settings or {}).get("employee_builder") or {}
        spec = builder.get("compiled_spec") or {}
        if (spec.get("delivery") or {}).get("provisioning_version") != 1:
            setup_blockers.append("Compile the employee to provision its action contracts before Go Live")
        else:
            managed_keys = {
                item["key"] for item in spec.get("requirements", [])
                if item.get("status") == "xvond_managed"
            }
            if managed_keys:
                assignment = db.query(AgentToolAssignment).filter(
                    AgentToolAssignment.agent_id == agent_id,
                    AgentToolAssignment.tool_name == "action_request",
                ).first()
                stored_actions = ((assignment.config or {}).get("actions") or {}) if assignment else {}
                if any(not isinstance(stored_actions.get(key), dict) for key in managed_keys):
                    setup_blockers.append("Compile the employee again to restore missing action contracts")
    if not profile_ready:
        setup_blockers.append("Complete employee identity and behavior")
    if not knowledge_ready:
        setup_blockers.append("Attach at least one enabled knowledge source")
    if not channels["configured"]:
        setup_blockers.append("Connect and configure at least one customer channel")
    setup_blockers.extend(actions["issues"])
    setup_blockers.extend(integration_issues)
    if workflow_required and not workflow_ready:
        setup_blockers.append("Workflow Engine is not ready for enabled business actions")

    setup_ready = not setup_blockers
    blockers = list(setup_blockers)
    if not company.active:
        blockers.insert(0, "Company must be active before this employee can go live")
    if not agent.enabled:
        blockers.insert(0, "AI employee is in draft mode")
    elif not channels["live"]:
        blockers.append("Activate at least one connected customer channel")
    elif not channels["customer_ready"]:
        blockers.append("Complete live channel acceptance before customer handover")

    ready_for_customer = bool(
        company.active
        and agent.enabled
        and setup_ready
        and channels["customer_ready"]
    )

    return {
        "agent": agent,
        "company": company,
        "payload": {
            "company_id": company_id,
            "agent_id": agent_id,
            "company_active": bool(company.active),
            "ready_for_customer": ready_for_customer,
            "setup_ready": setup_ready,
            "workflow_required": workflow_required,
            "lifecycle": "live" if agent.enabled else "draft",
            "mode": "conversational_and_operational" if actions["requested"] else "conversational",
            "blockers": blockers,
            "setup_blockers": setup_blockers,
            "checks": {
                "company_active": bool(company.active),
                "employee_enabled": bool(agent.enabled),
                "profile": profile_ready,
                "knowledge": knowledge_ready,
                "channels": channels["configured"],
                "live_channels": channels["live"],
                "customer_ready_channels": channels["customer_ready"],
                "actions": actions["ready"],
                "workflow_engine": workflow_ready,
                "connected_apps": not integration_issues,
            },
            "counts": {
                "knowledge_sources": knowledge_count,
                "channels": channels["configured_count"],
                "enabled_channels": channels["enabled_count"],
                "live_channels": channels["live_count"],
                "customer_ready_channels": channels["customer_ready_count"],
                "acceptance_pending_channels": channels["acceptance_pending_count"],
                "enabled_actions": actions["enabled_count"],
                "legacy_business_tools": len(actions["legacy_business_tools"]),
                "required_connected_apps": len(actions["required_integration_ids"]),
            },
        },
    }


@router.get("/companies/{company_id}/agents/{agent_id}")
def get_delivery_readiness(
    company_id: int,
    agent_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        return _delivery_state(db, company_id, agent_id)["payload"]
    finally:
        db.close()


@router.post("/companies/{company_id}/agents/{agent_id}/go-live")
def go_live(
    company_id: int,
    agent_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        state = _delivery_state(db, company_id, agent_id)
        agent = state["agent"]
        company = state["company"]
        if agent.enabled:
            return {**state["payload"], "status": "already_live"}
        if not state["payload"]["setup_ready"]:
            raise HTTPException(
                409,
                detail={
                    "message": "AI employee is not ready to go live",
                    "blockers": state["payload"]["setup_blockers"],
                },
            )
        if not company.active:
            raise HTTPException(
                409,
                detail={
                    "message": "Activate the company before the AI employee goes live",
                    "blockers": ["Company is inactive"],
                },
            )
        if state["payload"]["workflow_required"]:
            _assert_workflow_runtime_ready(company_id, agent_id)
        limits_service.check_agent_limit(db, company_id)
        agent.enabled = True
        db.commit()
        live = _delivery_state(db, company_id, agent_id)["payload"]
        return {**live, "status": "live"}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/companies/{company_id}/agents/{agent_id}/deactivate")
def deactivate(
    company_id: int,
    agent_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        state = _delivery_state(db, company_id, agent_id)
        agent = state["agent"]
        agent.enabled = False
        db.commit()
        draft = _delivery_state(db, company_id, agent_id)["payload"]
        return {**draft, "status": "draft"}
    finally:
        db.close()
