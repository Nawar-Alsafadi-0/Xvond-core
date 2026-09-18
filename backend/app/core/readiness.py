from datetime import UTC, datetime

from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.ai.routing_quality import set_quality_tier_cap
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import (
    configured_secret_fields,
    public_config,
    reveal_config,
)
from backend.app.core.error_safety import safe_error_label
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.company_profile import CompanyProfile
from backend.app.modules.ai_agent.factory_models import AgentConfig
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.profile_models import AIAgentProfile
from backend.app.modules.ai_agent.self_service_policy import (
    is_self_service_company,
    self_service_readiness,
)
from backend.app.modules.billing.service_models import ServicePlan, ServiceSubscription
from backend.app.modules.channels.acceptance import customer_roundtrip_verified
from backend.app.modules.channels.catalog import validate_channel_config
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state
from backend.app.modules.integrations.catalog import validate_integration_config
from backend.app.modules.integrations.models import CompanyIntegration
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument
from backend.app.modules.solutions.catalog import AI_AGENT_PACKAGE_QUALITY_CAPS
from backend.app.modules.tools.executor import tool_executor
from backend.app.modules.tools.models import AgentToolAssignment


LEGACY_BUSINESS_TOOL_NAMES = frozenset({"booking", "order", "lead"})


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def validate_config(
    validator,
    item_type: str,
    config: dict,
) -> tuple[bool, str | None]:
    try:
        validator(item_type, config or {})
        return True, None
    except ValueError as exc:
        # Validators contain Xvond-authored configuration messages, not provider
        # response bodies. Keep those actionable setup messages for Operations.
        return False, str(exc)


def _service_subscription(db, company_id: int):
    subscription = (
        db.query(ServiceSubscription)
        .filter(
            ServiceSubscription.company_id == company_id,
            ServiceSubscription.service_code == "ai_agents",
        )
        .first()
    )
    if subscription is None:
        return None, None, False
    plan = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.id == subscription.plan_id,
            ServicePlan.service_code == "ai_agents",
            ServicePlan.enabled.is_(True),
        )
        .first()
    )
    now = _utcnow_naive()
    active = bool(
        subscription.status == "active"
        and subscription.current_period_start <= now < subscription.current_period_end
        and plan is not None
    )
    return subscription, plan, active


def _plan_quality_cap(plan: ServicePlan | None) -> int | None:
    if plan is None:
        return None
    explicit = (plan.limits or {}).get("max_quality_tier")
    value = (
        explicit
        if explicit not in (None, "", 0, "0")
        else AI_AGENT_PACKAGE_QUALITY_CAPS.get(str(plan.tier or "").strip().lower())
    )
    if value in (None, "", 0, "0"):
        return None
    try:
        tier = int(value)
    except (TypeError, ValueError):
        return None
    return tier if 1 <= tier <= 4 else None


def _provider_runtime(
    db,
    company_id: int,
    agent: AIAgent,
    subscription_plan: ServicePlan | None = None,
):
    try:
        set_quality_tier_cap(_plan_quality_cap(subscription_plan))
        selections = runtime_selections(
            db,
            company_id,
            agent.provider,
            agent.model,
        )
    except Exception as exc:
        return [], safe_error_label(exc)
    finally:
        set_quality_tier_cap(None)
    real = [selection for selection in selections if selection.provider != "mock"]
    return real, None


def _channel_customer_accepted(
    *,
    channel_type: str,
    channel_config: dict,
    connected: bool,
    connection: dict | None,
) -> bool:
    """Return whether a live channel has evidence from the real customer path.

    Configuration and transport connectivity are prerequisites, not acceptance.
    Every channel needs a successful real round-trip marker. WhatsApp Business App
    Coexistence additionally needs an observed SMB echo so native human takeover is
    proven before the channel can count toward ``ready_for_customer``.
    """

    if not connected or not customer_roundtrip_verified(channel_config):
        return False
    if channel_type == "whatsapp" and channel_config.get("coexistence") is True:
        return bool(connection and connection.get("coexistence_ready") is True)
    return True


def company_readiness(db, company_id: int):
    """Return company setup readiness without conflating setup with live traffic.

    A company may be activated once at least one AI employee is fully configured in
    draft with a configured channel. Employee Go Live and channel activation are
    separate gates. This prevents the old circular dependency where a channel had
    to be live before the company could be activated while channel activation itself
    required an active company.
    """
    company = db.query(Company).filter(Company.id == company_id).first()
    if company is None:
        return None
    self_service_company = is_self_service_company(company)

    company_profile = (
        db.query(CompanyProfile)
        .filter(CompanyProfile.company_id == company_id)
        .first()
    )
    profile_missing = []
    if company_profile is None:
        profile_missing = [
            "business_type",
            "country",
            "timezone",
            "primary_language",
        ]
    else:
        for field in (
            "business_type",
            "country",
            "timezone",
            "primary_language",
        ):
            if not str(getattr(company_profile, field, None) or "").strip():
                profile_missing.append(field)
    company_profile_ready = not profile_missing

    modules = (
        db.query(CompanyModule)
        .filter(
            CompanyModule.company_id == company_id,
            CompanyModule.enabled.is_(True),
        )
        .all()
    )

    subscription, subscription_plan, subscription_ready = _service_subscription(
        db, company_id
    )

    agents = (
        db.query(AIAgent)
        .filter(AIAgent.company_id == company_id)
        .order_by(AIAgent.id.asc())
        .all()
    )

    integrations = (
        db.query(CompanyIntegration)
        .filter(
            CompanyIntegration.company_id == company_id,
            CompanyIntegration.enabled.is_(True),
        )
        .all()
    )
    integration_results = []
    for item in integrations:
        configured, error = validate_config(
            validate_integration_config,
            item.integration_type,
            reveal_config(item.config),
        )
        integration_results.append(
            {
                "id": item.id,
                "type": item.integration_type,
                "name": item.name,
                "enabled": item.enabled,
                "configured": configured,
                "config": public_config(item.config),
                "configured_secret_fields": configured_secret_fields(item.config),
                "issue": error,
            }
        )

    agent_results = []
    for agent in agents:
        self_service_state = None
        agent_config = None
        if self_service_company:
            agent_config = (
                db.query(AgentConfig)
                .filter(AgentConfig.agent_id == agent.id)
                .first()
            )
            if agent_config is not None:
                try:
                    self_service_state = self_service_readiness(
                        db,
                        company=company,
                        agent=agent,
                        config=agent_config,
                    )
                except Exception as exc:
                    self_service_state = {
                        "ready": False,
                        "channels_required": False,
                        "blockers": [safe_error_label(exc)],
                    }

        profile = (
            db.query(AIAgentProfile)
            .filter(
                AIAgentProfile.agent_id == agent.id,
                AIAgentProfile.company_id == company_id,
            )
            .first()
        )
        knowledge_count = (
            db.query(AgentKnowledge)
            .join(
                KnowledgeDocument,
                KnowledgeDocument.id == AgentKnowledge.document_id,
            )
            .filter(
                AgentKnowledge.agent_id == agent.id,
                AgentKnowledge.enabled.is_(True),
                KnowledgeDocument.enabled.is_(True),
            )
            .count()
        )
        tools = (
            db.query(AgentToolAssignment)
            .filter(
                AgentToolAssignment.agent_id == agent.id,
                AgentToolAssignment.enabled.is_(True),
            )
            .all()
        )
        enabled_tool_names = {str(item.tool_name or "").strip() for item in tools}
        legacy_business_tools = sorted(
            enabled_tool_names & LEGACY_BUSINESS_TOOL_NAMES
        )
        action_request_assigned = "action_request" in enabled_tool_names
        runtime_tools = tool_executor.get_agent_tools(db=db, agent_id=agent.id)
        ready_action = any(item.get("name") == "action_request" for item in runtime_tools)
        tools_ready = bool(
            not legacy_business_tools
            and (not action_request_assigned or ready_action)
        )

        channels = (
            db.query(AgentChannel)
            .filter(AgentChannel.agent_id == agent.id)
            .order_by(AgentChannel.id.asc())
            .all()
        )
        channel_results = []
        for channel in channels:
            channel_config = reveal_config(channel.config)
            configured, error = validate_config(
                validate_channel_config,
                channel.channel_type,
                channel_config,
            )
            connection = None
            if configured and channel.channel_type == "whatsapp":
                connection = whatsapp_connection_state(
                    channel_config,
                    verify_remote=True,
                )
            connected = bool(
                configured
                and (
                    channel.channel_type != "whatsapp"
                    or (connection and connection["connected"] is True)
                )
            )
            roundtrip_verified = customer_roundtrip_verified(channel_config)
            customer_accepted = _channel_customer_accepted(
                channel_type=channel.channel_type,
                channel_config=channel_config,
                connected=connected,
                connection=connection,
            )
            channel_results.append(
                {
                    "id": channel.id,
                    "type": channel.channel_type,
                    "enabled": channel.enabled,
                    "configured": configured,
                    "connected": connected,
                    "roundtrip_verified": roundtrip_verified,
                    "customer_accepted": customer_accepted,
                    "coexistence_ready": (
                        connection.get("coexistence_ready") if connection else None
                    ),
                    "connection_status": (
                        connection.get("connection_status") if connection else None
                    ),
                    "connection_issue": (
                        connection.get("connection_issue") if connection else error
                    ),
                    "config": public_config(channel.config),
                    "configured_secret_fields": configured_secret_fields(channel.config),
                    "issue": error,
                }
            )
        configured_channels = [item for item in channel_results if item["configured"]]
        enabled_channels = [item for item in channel_results if item["enabled"]]
        live_channels = [
            item
            for item in enabled_channels
            if item["connected"]
        ]
        unaccepted_enabled_channels = [
            item
            for item in enabled_channels
            if not item["customer_accepted"]
        ]

        provider_selections, provider_error = _provider_runtime(
            db,
            company_id,
            agent,
            subscription_plan if subscription_ready else None,
        )
        provider_ready = bool(provider_selections)
        prompt_ready = bool((agent.system_prompt or "").strip())
        employee_profile_ready = profile is not None

        issues = []
        warnings = []
        if not provider_ready:
            issues.append(
                "No real routed AI provider/model is available"
                + (f" ({provider_error})" if provider_error else "")
            )
        if not prompt_ready:
            issues.append("System prompt is empty")
        if not employee_profile_ready:
            issues.append("AI employee profile is missing")
        if not self_service_company:
            if knowledge_count == 0:
                issues.append("No enabled knowledge connected")
            if not channels:
                issues.append("No channel configured")
            elif not configured_channels:
                issues.append("Channel configuration is incomplete")
        elif self_service_state is not None:
            for blocker in self_service_state.get("blockers") or []:
                if blocker not in issues:
                    issues.append(blocker)
        if legacy_business_tools:
            issues.append(
                "Legacy business tools are still enabled: "
                + ", ".join(legacy_business_tools)
                + ". Configure canonical Business Actions before Go Live"
            )
        if action_request_assigned and not ready_action:
            issues.append(
                "Business Actions are enabled, but no configured customer action is runtime-ready"
            )
        if not agent.enabled:
            warnings.append("AI employee is in draft mode; activate the company, then use Go Live")
        elif (
            not live_channels
            and (
                not self_service_company
                or bool((self_service_state or {}).get("channels_required"))
            )
        ):
            warnings.append("AI employee is live but no customer channel is active")
        if any(
            item["type"] == "whatsapp"
            and item["enabled"]
            and not item["connected"]
            for item in channel_results
        ):
            warnings.append(
                "WhatsApp is enabled locally but Meta transport is not connected"
            )
        if any(
            item["enabled"]
            and item["connected"]
            and not item["roundtrip_verified"]
            for item in channel_results
        ):
            warnings.append(
                "Channel transport is live but a real customer round-trip has not been verified"
            )
        if any(
            item["type"] == "whatsapp"
            and item["enabled"]
            and item["connected"]
            and item["roundtrip_verified"]
            and item["coexistence_ready"] is False
            for item in channel_results
        ):
            warnings.append(
                "WhatsApp Coexistence transport and AI round-trip are live but human takeover acceptance is pending"
            )

        if self_service_company:
            setup_ready = bool(
                self_service_state
                and self_service_state.get("ready")
                and provider_ready
                and prompt_ready
                and employee_profile_ready
                and tools_ready
            )
            if bool((self_service_state or {}).get("channels_required")):
                ready_for_customer = bool(
                    company.active
                    and agent.enabled
                    and setup_ready
                    and enabled_channels
                    and live_channels
                    and not unaccepted_enabled_channels
                )
            else:
                ready_for_customer = bool(
                    company.active
                    and agent.enabled
                    and setup_ready
                )
        else:
            setup_ready = bool(
                provider_ready
                and prompt_ready
                and employee_profile_ready
                and knowledge_count > 0
                and configured_channels
                and tools_ready
            )
            ready_for_customer = bool(
                company.active
                and agent.enabled
                and setup_ready
                and enabled_channels
                and live_channels
                and not unaccepted_enabled_channels
            )
        agent_results.append(
            {
                "id": agent.id,
                "name": agent.name,
                "enabled": agent.enabled,
                "provider": agent.provider,
                "model": agent.model,
                "provider_ready": provider_ready,
                "provider_routes": [
                    {
                        "provider": selection.provider,
                        "model": selection.model,
                        "reason": selection.reason,
                    }
                    for selection in provider_selections
                ],
                "package_max_quality_tier": (
                    _plan_quality_cap(subscription_plan) if subscription_ready else None
                ),
                "prompt_ready": prompt_ready,
                "profile_exists": employee_profile_ready,
                "knowledge_count": knowledge_count,
                "tool_count": len(tools),
                "tools_ready": tools_ready,
                "action_request_assigned": action_request_assigned,
                "ready_action": ready_action,
                "legacy_business_tools": legacy_business_tools,
                "channels": channel_results,
                "configured_channel_count": len(configured_channels),
                "enabled_channel_count": len(enabled_channels),
                "live_channel_count": len(live_channels),
                "unaccepted_enabled_channel_count": len(unaccepted_enabled_channels),
                "setup_ready": setup_ready,
                "ready": setup_ready,
                "ready_for_customer": ready_for_customer,
                "issues": issues,
                "warnings": warnings,
                "delivery_mode": (
                    "self_service" if self_service_company else "managed"
                ),
                "self_service_readiness": self_service_state,
            }
        )

    setup_ready_agents = [item for item in agent_results if item["setup_ready"]]
    live_agents = [item for item in agent_results if item["ready_for_customer"]]
    company_issues = []
    company_warnings = []

    if not self_service_company and not company_profile_ready:
        company_issues.append(
            "Company profile is incomplete: " + ", ".join(profile_missing)
        )
    if not subscription_ready:
        company_issues.append("No active AI Agents service subscription")
    if not modules:
        company_issues.append("No active modules")
    if not agents:
        company_issues.append("No AI employees")
    elif not setup_ready_agents:
        company_issues.append("No setup-ready AI employee")
    if not company.active:
        company_warnings.append("Company runtime is currently stopped")
    elif not live_agents:
        company_warnings.append(
            "Company runtime is active but at least one enabled customer path still needs live acceptance"
        )

    setup_ready = bool(
        subscription_ready
        and modules
        and setup_ready_agents
        and (company_profile_ready or self_service_company)
    )
    if setup_ready and company.active:
        status = "ACTIVE"
    elif setup_ready:
        status = "READY_TO_ACTIVATE"
    else:
        status = "SETUP_REQUIRED"

    subscription_data = None
    if subscription is not None:
        effective_status = subscription.status
        if (
            effective_status == "active"
            and subscription.current_period_end <= _utcnow_naive()
        ):
            effective_status = "expired"
        subscription_data = {
            "id": subscription.id,
            "plan_id": subscription.plan_id,
            "plan_name": subscription_plan.name if subscription_plan else None,
            "plan_tier": subscription_plan.tier if subscription_plan else None,
            "max_quality_tier": _plan_quality_cap(subscription_plan),
            "status": effective_status,
            "current_period_start": subscription.current_period_start,
            "current_period_end": subscription.current_period_end,
            "service_code": "ai_agents",
        }

    return {
        "company": {
            "id": company.id,
            "name": company.name,
            "active": company.active,
            "lifecycle_status": company.lifecycle_status,
            "lifecycle_updated_at": company.lifecycle_updated_at,
        },
        "company_profile_ready": company_profile_ready,
        "profile_ready": company_profile_ready,
        "delivery_mode": "self_service" if self_service_company else "managed",
        "company_profile_missing": profile_missing,
        "ready": setup_ready,
        "setup_ready": setup_ready,
        "ready_for_customer": bool(live_agents),
        "status": status,
        "subscription_ready": subscription_ready,
        "subscription": subscription_data,
        "modules": [item.module_name for item in modules],
        "agents": agent_results,
        "integrations": integration_results,
        "issues": company_issues,
        "warnings": company_warnings,
        "runtime_environment": settings.APP_ENV,
    }