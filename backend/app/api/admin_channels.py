from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.config_secrets import (
    configured_secret_fields,
    merge_config,
    public_config,
    reveal_config,
)
from backend.app.core.database.connection import SessionLocal
from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.core.dependencies import require_xvond_admin, require_xvond_operator
from backend.app.models.company import Company
from backend.app.models.company_module import CompanyModule
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.audit.service import audit_service
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.channels.catalog import (
    CHANNEL_RUNTIME_LIVE,
    CHANNEL_SETUP_MANAGED,
    N8N_CHANNEL_ADAPTER,
    canonical_channel_type,
    get_channel_capability,
    get_channel_definition,
    validate_channel_config,
)
from backend.app.modules.channels.delivery import deactivate_managed_channel_route
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state
from backend.app.modules.knowledge.models import AgentKnowledge, KnowledgeDocument

router = APIRouter(prefix="/admin/channels", tags=["Xvond Admin - Channels"])


class ChannelCreate(BaseModel):
    channel_type: str
    config: dict = Field(default_factory=dict)


class ChannelUpdate(BaseModel):
    config: dict | None = None
    enabled: bool | None = None


class ManagedChannelConnect(BaseModel):
    connection_key: str = Field(min_length=1, max_length=200)
    provider_account_label: str | None = Field(default=None, max_length=200)
    channel_instructions: str | None = Field(default=None, max_length=4000)
    # Optional one-shot provisioning payload. Secrets are forwarded directly to
    # the isolated workflow plane and are never merged into AgentChannel.config.
    provider_type: str | None = Field(default=None, max_length=80)
    provider_url: str | None = Field(default=None, max_length=2000)
    provider_secret: str | None = Field(default=None, min_length=32, max_length=4000)
    provider_config: dict | None = None


class WhatsAppConfigUpdate(BaseModel):
    phone_number_id: str | None = None
    access_token: str | None = None
    verify_token: str | None = None
    app_secret: str | None = None
    graph_api_version: str | None = None
    # Accepted only so older clients do not break. These fields are ignored:
    # AI Employee profile is the single source of truth for language/dialect.
    language: str | None = None
    dialect: str | None = None
    tone: str | None = None
    response_style: str | None = None
    response_length: str | None = None
    emoji_style: str | None = None
    channel_instructions: str | None = None


def _channel_configured(
    channel: AgentChannel,
    channel_config: dict | None = None,
) -> bool:
    try:
        validate_channel_config(
            channel.channel_type,
            reveal_config(channel.config) if channel_config is None else channel_config,
        )
        return True
    except ValueError:
        return False


def _assert_unique_whatsapp_phone_number_id(
    db,
    phone_number_id: str | None,
    *,
    exclude_channel_id: int | None = None,
) -> None:
    """Guarantee deterministic WhatsApp routing across the whole platform."""
    target = str(phone_number_id or "").strip()
    if not target:
        return

    query = db.query(AgentChannel).filter(AgentChannel.channel_type == "whatsapp")
    if exclude_channel_id is not None:
        query = query.filter(AgentChannel.id != exclude_channel_id)

    for existing in query.all():
        config = reveal_config(existing.config) or {}
        if str(config.get("phone_number_id") or "").strip() == target:
            raise HTTPException(
                409,
                "This WhatsApp phone number is already assigned to another AI Employee",
            )


def serialize_channel(
    channel: AgentChannel,
    *,
    verify_connection: bool = False,
) -> dict:
    channel_config = reveal_config(channel.config)
    configured = _channel_configured(channel, channel_config)
    if channel.channel_type == "whatsapp":
        connection = whatsapp_connection_state(
            channel_config,
            verify_remote=verify_connection,
        )
    else:
        connection = {
            "connected": configured,
            "meta_onboarding_complete": False,
            "connection_status": "connected" if configured else "incomplete",
            "connection_issue": (
                None if configured else "Channel configuration is incomplete."
            ),
            "connection_checked_at": None,
            "meta_error_code": None,
        }

    capability = get_channel_capability(channel.channel_type) or {}
    if capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE:
        connection = {
            "connected": False,
            "meta_onboarding_complete": False,
            "connection_status": "adapter_required",
            "connection_issue": "Xvond runtime adapter is not available for this channel yet.",
            "connection_checked_at": None,
            "meta_error_code": None,
        }

    return {
        "id": channel.id,
        "company_id": channel.company_id,
        "agent_id": channel.agent_id,
        "channel_type": canonical_channel_type(channel.channel_type),
        "channel_name": capability.get("name") or channel.channel_type,
        "setup_mode": capability.get("setup_mode"),
        "runtime_state": capability.get("runtime_state"),
        "runtime_adapter": capability.get("runtime_adapter"),
        "config": public_config(channel.config),
        "configured_secret_fields": configured_secret_fields(channel.config),
        "configured": configured,
        **connection,
        "connection_method": channel_config.get("connection_method"),
        "coexistence": bool(channel_config.get("coexistence")),
        "enabled": channel.enabled,
        "created_at": channel.created_at,
    }


def _managed_gateway_capability(channel: AgentChannel) -> dict:
    capability = get_channel_capability(channel.channel_type) or {}
    if (
        capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
        or capability.get("setup_mode") != CHANNEL_SETUP_MANAGED
        or capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
    ):
        raise HTTPException(
            409,
            "This channel is not provisioned through the Xvond managed channel gateway",
        )
    return capability


def _verify_managed_gateway_route(
    channel: AgentChannel,
    *,
    connection_key: str,
) -> dict:
    if not n8n_gateway.configured():
        raise HTTPException(503, "Xvond managed channel gateway is not configured")
    try:
        result = n8n_gateway.execute(
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            action="channel.check",
            data={
                "channel_id": channel.id,
                "channel_type": canonical_channel_type(channel.channel_type),
                "connection_key": connection_key,
            },
        )
    except N8NGatewayError as exc:
        raise HTTPException(
            502,
            "Xvond managed channel route could not be verified",
        ) from exc

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if result.get("success") is not True or data.get("configured") is not True:
        raise HTTPException(
            409,
            "The managed channel provider route is not configured yet",
        )
    return data


def _ensure_channels_module(db, company_id: int):
    module = db.query(CompanyModule).filter(
        CompanyModule.company_id == company_id,
        CompanyModule.module_name == "channels",
    ).first()
    if module is None:
        module = CompanyModule(
            company_id=company_id,
            module_name="channels",
            enabled=True,
        )
        db.add(module)
    else:
        module.enabled = True
    return module


def _audit_channel(
    db,
    admin: User,
    channel: AgentChannel,
    action: str,
    *,
    details: dict | None = None,
) -> None:
    audit_service.log(
        db=db,
        action=action,
        resource_type="channel",
        resource_id=channel.id,
        user_id=admin.id,
        company_id=channel.company_id,
        details={
            "agent_id": channel.agent_id,
            "channel_type": channel.channel_type,
            "enabled": channel.enabled,
            **(details or {}),
        },
    )


def _has_real_runtime_provider(
    db,
    company_id: int,
    agent: AIAgent | None,
) -> bool:
    if agent is None:
        return False
    try:
        selections = runtime_selections(
            db,
            company_id,
            agent.provider,
            agent.model,
        )
    except Exception:
        return False
    return any(item.provider != "mock" for item in selections)


def _useful_business_knowledge(document: KnowledgeDocument) -> bool:
    content = (document.content or "").strip()
    if len(content) < 20:
        return False
    if document.source_type != "business_profile":
        return True
    markers = (
        "Description:",
        "Working Hours:",
        "Locations / Branches:",
        "Services:",
        "Policies:",
        "Business Rules:",
    )
    return len(content) >= 80 and any(marker in content for marker in markers)


def _activation_blockers(db, channel: AgentChannel) -> list[str]:
    blockers = []
    capability = get_channel_capability(channel.channel_type)
    if capability is None:
        return ["Channel type is not registered"]
    if capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE:
        return [
            f"{capability.get('name') or channel.channel_type}: Xvond runtime adapter is not available yet"
        ]

    company = db.query(Company).filter(Company.id == channel.company_id).first()
    agent = db.query(AIAgent).filter(
        AIAgent.id == channel.agent_id,
        AIAgent.company_id == channel.company_id,
    ).first()
    if company is None or not company.active:
        blockers.append("Company must be active")
    if agent is None or not agent.enabled:
        blockers.append("AI employee must be active")
    elif not _has_real_runtime_provider(db, channel.company_id, agent):
        blockers.append(
            "At least one real AI provider/model must be enabled and configured"
        )

    channel_config = reveal_config(channel.config)
    configured = _channel_configured(channel, channel_config)
    channel_type = canonical_channel_type(channel.channel_type)
    if not configured:
        blockers.append(
            f"{capability.get('name') or channel_type.title()} channel configuration is incomplete"
        )
    elif channel_type == "whatsapp":
        connection = whatsapp_connection_state(
            channel_config,
            verify_remote=True,
        )
        if connection["connected"] is not True:
            blockers.append(
                connection.get("connection_issue")
                or "WhatsApp must be connected and verified with Meta"
            )
    elif channel_type == "voice":
        required = (
            "vapi_assistant_id",
            "vapi_phone_number_id",
            "vapi_llm_credential_id",
            "llm_api_key",
        )
        if str(channel_config.get("provisioning_state") or "").strip().lower() != "connected":
            blockers.append("Voice: Xvond managed provisioning is not complete")
        elif any(not str(channel_config.get(item) or "").strip() for item in required):
            blockers.append("Voice: Vapi provisioning evidence is incomplete")
    elif capability.get("runtime_adapter") == N8N_CHANNEL_ADAPTER:
        if not n8n_gateway.configured():
            blockers.append("Xvond managed channel gateway is not configured")
        else:
            try:
                route_check = n8n_gateway.execute(
                    company_id=channel.company_id,
                    agent_id=channel.agent_id,
                    action="channel.check",
                    data={
                        "channel_id": channel.id,
                        "connection_key": str(channel_config.get("connection_key") or "").strip(),
                    },
                )
            except N8NGatewayError:
                blockers.append("Xvond managed channel route could not be verified")
            else:
                if (
                    route_check.get("success") is not True
                    or not isinstance(route_check.get("data"), dict)
                    or route_check["data"].get("configured") is not True
                ):
                    blockers.append("Xvond managed channel route is not configured")

    docs = (
        db.query(KnowledgeDocument)
        .join(AgentKnowledge, AgentKnowledge.document_id == KnowledgeDocument.id)
        .filter(
            KnowledgeDocument.company_id == channel.company_id,
            KnowledgeDocument.enabled.is_(True),
            AgentKnowledge.agent_id == channel.agent_id,
            AgentKnowledge.enabled.is_(True),
        )
        .all()
    )
    if not any(_useful_business_knowledge(doc) for doc in docs):
        blockers.append("Add real business knowledge before activating this channel")
    return blockers


@router.post("/agents/{agent_id}")
def create_channel(
    agent_id: int,
    data: ChannelCreate,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if agent is None:
            raise HTTPException(404, "AI Agent not found")
        channel_type = canonical_channel_type(data.channel_type)
        if not channel_type:
            raise HTTPException(400, "Channel type is required")
        if get_channel_definition(channel_type) is None:
            raise HTTPException(400, "Unsupported channel type")
        if db.query(AgentChannel).filter(
            AgentChannel.agent_id == agent.id,
            AgentChannel.channel_type == channel_type,
        ).first() is not None:
            raise HTTPException(
                409,
                "This channel type is already assigned to the agent",
            )
        if channel_type == "whatsapp":
            _assert_unique_whatsapp_phone_number_id(
                db,
                data.config.get("phone_number_id"),
            )
        channel = AgentChannel(
            company_id=agent.company_id,
            agent_id=agent.id,
            channel_type=channel_type,
            config=data.config,
            enabled=False,
        )
        db.add(channel)
        db.flush()
        _ensure_channels_module(db, agent.company_id)
        _audit_channel(db, current_admin, channel, "channel.created")
        db.commit()
        db.refresh(channel)
        result = serialize_channel(channel)
        result["status"] = "created"
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/companies/{company_id}")
def list_company_channels(
    company_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        items = db.query(AgentChannel).filter(
            AgentChannel.company_id == company_id
        ).order_by(AgentChannel.id.asc()).all()
        return {
            "company_id": company_id,
            "channels": [
                serialize_channel(
                    item,
                    verify_connection=item.channel_type == "whatsapp",
                )
                for item in items
            ],
        }
    finally:
        db.close()


@router.get("/managed/requests")
def managed_channel_requests(
    current_admin: User = Depends(require_xvond_operator),
):
    """Return the global queue of unresolved Xvond-managed channel requests."""

    db = SessionLocal()
    try:
        channels = (
            db.query(AgentChannel)
            .filter(AgentChannel.enabled.is_(False))
            .order_by(AgentChannel.created_at.asc(), AgentChannel.id.asc())
            .all()
        )
        company_ids = {row.company_id for row in channels}
        agent_ids = {row.agent_id for row in channels}
        companies = {
            row.id: row
            for row in (
                db.query(Company).filter(Company.id.in_(company_ids)).all()
                if company_ids
                else []
            )
        }
        agents = {
            row.id: row
            for row in (
                db.query(AIAgent).filter(AIAgent.id.in_(agent_ids)).all()
                if agent_ids
                else []
            )
        }

        requests = []
        for channel in channels:
            capability = get_channel_capability(channel.channel_type) or {}
            if (
                capability.get("runtime_state") != CHANNEL_RUNTIME_LIVE
                or capability.get("setup_mode") != CHANNEL_SETUP_MANAGED
                or capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
            ):
                continue
            config = reveal_config(channel.config) or {}
            state = str(config.get("provisioning_state") or "").strip().lower()
            if state != "requested":
                continue
            company = companies.get(channel.company_id)
            agent = agents.get(channel.agent_id)
            requests.append(
                {
                    "channel_id": channel.id,
                    "company_id": channel.company_id,
                    "company_name": company.name if company else None,
                    "agent_id": channel.agent_id,
                    "agent_name": agent.name if agent else None,
                    "channel_type": canonical_channel_type(channel.channel_type),
                    "channel_name": capability.get("name") or channel.channel_type,
                    "provisioning_state": state,
                    "request_source": config.get("request_source"),
                    "created_at": channel.created_at,
                }
            )
        return {"requests": requests, "count": len(requests)}
    finally:
        db.close()


@router.post("/{channel_id}/managed-connect")
def connect_managed_channel(
    channel_id: int,
    data: ManagedChannelConnect,
    current_admin: User = Depends(require_xvond_admin),
):
    """Verify and complete one Xvond-managed channel request.

    Provider credentials remain in the workflow/provider plane. Core stores only
    the provider-neutral connection key and verification evidence.
    """

    db = SessionLocal()
    try:
        channel = (
            db.query(AgentChannel)
            .filter(AgentChannel.id == channel_id)
            .with_for_update()
            .first()
        )
        if channel is None:
            raise HTTPException(404, "Channel not found")
        if channel.enabled:
            raise HTTPException(
                409,
                "Live managed channel routing cannot be changed in place; deactivate it before changing the provider route",
            )

        capability = _managed_gateway_capability(channel)
        connection_key = str(data.connection_key or "").strip()

        provision_fields = (
            data.provider_type,
            data.provider_url,
            data.provider_secret,
            data.provider_config,
        )
        wants_provisioning = any(value is not None for value in provision_fields)
        if wants_provisioning:
            if capability.get("packaged_provider") is not True:
                raise HTTPException(
                    409,
                    "This managed channel does not have a packaged Xvond provider binding yet",
                )
            if not all(value is not None for value in provision_fields):
                raise HTTPException(
                    400,
                    "provider_type, provider_url, provider_secret and provider_config are required together",
                )
            try:
                provisioned = n8n_gateway.provision_channel(
                    company_id=channel.company_id,
                    agent_id=channel.agent_id,
                    channel_id=channel.id,
                    channel_type=canonical_channel_type(channel.channel_type),
                    connection_key=connection_key,
                    provider_type=str(data.provider_type or "").strip(),
                    provider_url=str(data.provider_url or "").strip(),
                    provider_secret=str(data.provider_secret or ""),
                    provider_config=dict(data.provider_config or {}),
                    provider_account_label=(
                        str(data.provider_account_label or "").strip() or None
                    ),
                )
            except N8NGatewayError as exc:
                raise HTTPException(
                    502,
                    "Xvond managed channel route could not be provisioned",
                ) from exc
            if provisioned.get("success") is not True:
                raise HTTPException(
                    409,
                    "The managed channel provider route could not be provisioned",
                )

        verification = _verify_managed_gateway_route(
            channel,
            connection_key=connection_key,
        )

        incoming = {
            "connection_key": connection_key,
            "provisioning_state": "connected",
            "provisioning_error": None,
            "registry_cleanup_state": "active",
            "connection_method": "xvond_managed_gateway",
            "provisioning_verified_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        if data.provider_account_label is not None:
            incoming["provider_account_label"] = str(data.provider_account_label or "").strip() or None
        if data.channel_instructions is not None:
            incoming["channel_instructions"] = str(data.channel_instructions or "").strip() or None

        channel.config = merge_config(channel.config, incoming)
        channel.enabled = False
        _ensure_channels_module(db, channel.company_id)
        _audit_channel(
            db,
            current_admin,
            channel,
            "channel.managed_connected",
            details={
                "connection_method": "xvond_managed_gateway",
                "gateway_configured": True,
                "provider_account_label": incoming.get("provider_account_label"),
            },
        )
        db.flush()

        blockers = _activation_blockers(db, channel)
        db.commit()
        db.refresh(channel)
        result = serialize_channel(channel)
        result.update(
            {
                "status": "connected",
                "gateway_verified": True,
                "ready": not blockers,
                "blockers": blockers,
                "provider": {
                    "configured": verification.get("configured") is True,
                },
            }
        )
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/{channel_id}/readiness")
def channel_readiness(
    channel_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = db.query(AgentChannel).filter(
            AgentChannel.id == channel_id
        ).first()
        if channel is None:
            raise HTTPException(404, "Channel not found")
        blockers = _activation_blockers(db, channel)
        return {
            "channel_id": channel.id,
            "ready": not blockers,
            "blockers": blockers,
        }
    finally:
        db.close()


@router.put("/{channel_id}")
def update_channel(
    channel_id: int,
    data: ChannelUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = db.query(AgentChannel).filter(
            AgentChannel.id == channel_id
        ).first()
        if channel is None:
            raise HTTPException(404, "Channel not found")
        changed_fields = []
        previous_enabled = channel.enabled
        if data.config is not None:
            new_config = merge_config(channel.config, data.config)
            if channel.channel_type == "whatsapp":
                plain = reveal_config(new_config) or {}
                _assert_unique_whatsapp_phone_number_id(
                    db,
                    plain.get("phone_number_id"),
                    exclude_channel_id=channel.id,
                )
            channel.config = new_config
            changed_fields.append("config")
            db.flush()
        if data.enabled is True and channel.enabled is False:
            limits_service.check_channel_limit(db, channel.company_id)
            blockers = _activation_blockers(db, channel)
            if blockers:
                raise HTTPException(
                    409,
                    "Channel is not ready: " + "; ".join(blockers),
                )
            _ensure_channels_module(db, channel.company_id)
            channel.enabled = True
            changed_fields.append("enabled")
        elif data.enabled is False and channel.enabled is not False:
            channel.enabled = False
            changed_fields.append("enabled")
        if changed_fields:
            _audit_channel(
                db,
                current_admin,
                channel,
                "channel.updated",
                details={
                    "changed_fields": changed_fields,
                    "previous_enabled": previous_enabled,
                },
            )
        db.commit()
        db.refresh(channel)
        result = serialize_channel(channel)
        result["status"] = "updated"
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.put("/{channel_id}/whatsapp-config")
def configure_whatsapp(
    channel_id: int,
    data: WhatsAppConfigUpdate,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = db.query(AgentChannel).filter(
            AgentChannel.id == channel_id,
            AgentChannel.channel_type == "whatsapp",
        ).first()
        if channel is None:
            raise HTTPException(404, "WhatsApp channel not found")

        incoming = {
            key: value
            for key, value in data.model_dump().items()
            if value is not None
        }
        incoming.pop("language", None)
        incoming.pop("dialect", None)
        new_config = merge_config(channel.config, incoming)
        plain_config = reveal_config(new_config)
        try:
            validate_channel_config("whatsapp", plain_config)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        _assert_unique_whatsapp_phone_number_id(
            db,
            plain_config.get("phone_number_id"),
            exclude_channel_id=channel.id,
        )

        previous_enabled = channel.enabled
        channel.config = new_config
        channel.enabled = False
        _ensure_channels_module(db, channel.company_id)
        _audit_channel(
            db,
            current_admin,
            channel,
            "channel.whatsapp_configured",
            details={
                "changed_fields": sorted(incoming),
                "previous_enabled": previous_enabled,
                "configured_secret_fields": sorted(configured_secret_fields(new_config)),
            },
        )
        db.commit()
        db.refresh(channel)
        blockers = _activation_blockers(db, channel)
        result = serialize_channel(channel)
        result.update(
            {
                "status": "configured",
                "ready": not blockers,
                "blockers": blockers,
            }
        )
        return result
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.delete("/{channel_id}")
def delete_channel(
    channel_id: int,
    current_admin: User = Depends(require_xvond_admin),
):
    db = SessionLocal()
    try:
        channel = db.query(AgentChannel).filter(
            AgentChannel.id == channel_id
        ).first()
        if channel is None:
            raise HTTPException(404, "Channel not found")

        cleanup = deactivate_managed_channel_route(channel)
        if cleanup.get("required") is True and cleanup.get("complete") is not True:
            raise HTTPException(
                502,
                "Managed channel route cleanup could not be confirmed; channel was not deleted",
            )

        _audit_channel(
            db,
            current_admin,
            channel,
            "channel.deleted",
            details={
                "managed_route_cleanup": cleanup.get("reason"),
                "managed_route_deactivated": cleanup.get("deactivated"),
            },
        )
        db.delete(channel)
        db.commit()
        return {"status": "deleted", "channel_id": channel_id}
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
