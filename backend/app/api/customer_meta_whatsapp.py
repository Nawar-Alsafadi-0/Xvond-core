from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.app.api.admin_channels import (
    _activation_blockers,
    _assert_unique_whatsapp_phone_number_id,
    _ensure_channels_module,
)
from backend.app.api.admin_meta_whatsapp import (
    WHATSAPP_BEHAVIOR_DEFAULTS,
    _ensure_meta_configured,
    _exchange_code_for_token,
    _meta_settings,
    _missing_meta_settings,
    _resolve_signup_phone,
    _subscribe_app_to_waba,
    _coexistence_subscription_evidence,
)
from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.models.company import Company
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.ai_agent.self_service_policy import (
    assert_self_service_channel_selected,
    is_self_service_company,
)
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.catalog import validate_channel_config
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_connection import (
    META_CONNECTION_METHODS,
    whatsapp_connection_state,
    whatsapp_meta_onboarding_complete,
)


router = APIRouter(
    prefix="/customer/meta/whatsapp",
    tags=["Customer - Meta WhatsApp"],
)

_META_CONNECTION_METHODS = META_CONNECTION_METHODS


class CustomerEmbeddedSignupComplete(BaseModel):
    agent_id: int
    code: str
    waba_id: str
    phone_number_id: str | None = None
    business_id: str | None = None
    connection_mode: str | None = None


class CustomerWhatsAppDisconnect(BaseModel):
    agent_id: int


class CustomerWhatsAppSettingsUpdate(BaseModel):
    agent_id: int
    tone: str | None = None
    response_style: str | None = None
    response_length: str | None = None
    emoji_style: str | None = None
    channel_instructions: str | None = None


def _customer_agent(db, current_user: User, agent_id: int) -> AIAgent:
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == current_user.company_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(status_code=404, detail="AI Employee not found")
    return agent


def _assert_whatsapp_selected_for_self_service(db, agent: AIAgent) -> None:
    company = db.query(Company).filter(Company.id == agent.company_id).first()
    assert_self_service_channel_selected(
        db,
        company=company,
        agent=agent,
        channel_type="whatsapp",
    )


def _self_service_whatsapp_can_edit(db, agent: AIAgent) -> bool:
    company = db.query(Company).filter(Company.id == agent.company_id).first()
    return not (is_self_service_company(company) and agent.enabled)


def _require_self_service_whatsapp_editable(db, agent: AIAgent) -> None:
    if not _self_service_whatsapp_can_edit(db, agent):
        raise HTTPException(
            status_code=409,
            detail="Deactivate this employee before changing its WhatsApp connection",
        )


def _meta_channel_connected(
    channel: AgentChannel | None,
    channel_config: dict,
) -> bool:
    return channel is not None and whatsapp_meta_onboarding_complete(channel_config)


@router.get("/embedded-signup/config")
def embedded_signup_config(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = _customer_agent(db, current_user, agent_id)
        _assert_whatsapp_selected_for_self_service(db, agent)
        meta = _meta_settings()
        missing = _missing_meta_settings(meta)
        ready = not missing
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "whatsapp",
            )
            .first()
        )
        channel_config = (
            reveal_config(channel.config) if channel is not None else {}
        )
        configured = False
        if channel is not None:
            try:
                validate_channel_config("whatsapp", channel_config)
                configured = True
            except ValueError:
                pass
        connection = whatsapp_connection_state(
            channel_config,
            verify_remote=True,
        )
        connected = channel is not None and connection["connected"]
        blockers = (
            _activation_blockers(db, channel)
            if channel is not None
            else []
        )
        enabled = bool(channel.enabled) if channel is not None else False
        coexistence = bool(channel_config.get("coexistence"))
        return {
            "ready": ready,
            "can_edit": _self_service_whatsapp_can_edit(db, agent),
            "agent_id": agent.id,
            "company_id": agent.company_id,
            "app_id": meta["app_id"] if ready else None,
            "config_id": meta["config_id"] if ready else None,
            "graph_api_version": meta["graph_api_version"],
            "feature_type": meta.get("feature_type") or None,
            "session_info_version": meta.get("session_info_version") or None,
            "missing_settings": missing,
            "channel_id": channel.id if channel is not None else None,
            "configured": configured,
            "connected": connected,
            "enabled": enabled,
            "runtime_ready": bool(connected and enabled and not blockers),
            "blockers": blockers,
            "connection_method": channel_config.get("connection_method"),
            "coexistence": coexistence,
            "coexistence_ready": (
                bool(connection.get("coexistence_ready")) if coexistence else None
            ),
            "echo_received": (
                bool(connection.get("echo_received")) if coexistence else None
            ),
            "connection_status": connection["connection_status"],
            "connection_issue": connection["connection_issue"],
            "connection_checked_at": connection["connection_checked_at"],
            "meta_error_code": connection["meta_error_code"],
            "display_phone_number": channel_config.get("display_phone_number"),
            "verified_name": channel_config.get("verified_name"),
        }
    finally:
        db.close()


@router.post("/embedded-signup/complete")
def complete_embedded_signup(
    data: CustomerEmbeddedSignupComplete,
    current_user: User = Depends(require_customer_manager),
):
    config = _ensure_meta_configured()
    code = data.code.strip()
    waba_id = data.waba_id.strip()
    requested_phone_number_id = str(data.phone_number_id or "").strip() or None
    connection_mode = str(data.connection_mode or "embedded_signup").strip()
    if connection_mode not in {"embedded_signup", "coexistence"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid WhatsApp connection mode",
        )
    if not code or not waba_id:
        raise HTTPException(
            status_code=400,
            detail="code and waba_id are required",
        )

    # Validate tenant ownership before exchanging any Meta authorization code.
    db = SessionLocal()
    try:
        agent = _customer_agent(db, current_user, data.agent_id)
        _assert_whatsapp_selected_for_self_service(db, agent)
        _require_self_service_whatsapp_editable(db, agent)
        agent_id = agent.id
        company_id = agent.company_id
    finally:
        db.close()

    access_token = _exchange_code_for_token(code, config)
    phone = _resolve_signup_phone(
        waba_id=waba_id,
        phone_number_id=requested_phone_number_id,
        access_token=access_token,
        graph_api_version=config["graph_api_version"],
    )
    phone_number_id = str(phone.get("id") or "").strip()
    if not phone_number_id:
        raise HTTPException(
            status_code=502,
            detail="Meta did not return a usable phone number ID",
        )

    _subscribe_app_to_waba(
        waba_id=waba_id,
        access_token=access_token,
        graph_api_version=config["graph_api_version"],
    )
    evidence = {"waba_subscription_verified": True, "meta_app_id": config["app_id"]}
    if connection_mode == "coexistence":
        evidence.update(_coexistence_subscription_evidence(config))

    db = SessionLocal()
    try:
        # Re-check ownership in case the account changed while Meta signup was open.
        agent = _customer_agent(db, current_user, agent_id)
        db.refresh(agent, with_for_update=True)
        _assert_whatsapp_selected_for_self_service(db, agent)
        _require_self_service_whatsapp_editable(db, agent)
        if agent.company_id != company_id:
            raise HTTPException(
                status_code=409,
                detail="AI Employee company changed during WhatsApp setup",
            )

        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "whatsapp",
            )
            .with_for_update()
            .first()
        )
        _assert_unique_whatsapp_phone_number_id(
            db,
            phone_number_id,
            exclude_channel_id=channel.id if channel is not None else None,
        )
        method = (
            "meta_embedded_signup_coexistence"
            if connection_mode == "coexistence"
            else "meta_embedded_signup"
        )
        incoming = {
            **evidence,
            "waba_id": waba_id,
            "meta_business_id": data.business_id,
            "phone_number_id": phone_number_id,
            "display_phone_number": phone.get("display_phone_number"),
            "verified_name": phone.get("verified_name"),
            "access_token": access_token,
            "verify_token": config["verify_token"],
            "app_secret": config["app_secret"],
            "graph_api_version": config["graph_api_version"],
            "connection_method": method,
            "coexistence": connection_mode == "coexistence",
            "coexistence_echo_received_at": None,
            "activation_pending_coexistence": connection_mode == "coexistence",
        }
        if channel is None:
            incoming.update(WHATSAPP_BEHAVIOR_DEFAULTS)

        merged = merge_config(channel.config if channel else {}, incoming)
        validate_channel_config("whatsapp", reveal_config(merged))

        if channel is None:
            channel = AgentChannel(
                company_id=company_id,
                agent_id=agent.id,
                channel_type="whatsapp",
                config=merged,
                enabled=False,
            )
            db.add(channel)
        else:
            channel.config = merged
            channel.enabled = False

        _ensure_channels_module(db, company_id)
        db.flush()
        blockers = _activation_blockers(db, channel)
        channel.enabled = not blockers
        connection = whatsapp_connection_state(
            reveal_config(channel.config),
            verify_remote=True,
        )
        coexistence_ready = (
            bool(connection.get("coexistence_ready"))
            if connection_mode == "coexistence"
            else None
        )
        echo_received = (
            bool(connection.get("echo_received"))
            if connection_mode == "coexistence"
            else None
        )

        audit_service.log(
            db=db,
            action="whatsapp.customer_embedded_signup.connected",
            resource_type="agent_channel",
            resource_id=channel.id,
            user_id=current_user.id,
            company_id=company_id,
            details={
                "agent_id": agent.id,
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "connection_method": method,
                "coexistence": connection_mode == "coexistence",
                "coexistence_ready": coexistence_ready,
                "webhook_subscribed": True,
                "runtime_ready": not blockers,
                "blockers": blockers,
            },
        )
        db.commit()
        db.refresh(channel)
        return {
            "status": (
                "connected" if not blockers else "connected_needs_setup"
            ),
            "channel_id": channel.id,
            "company_id": company_id,
            "agent_id": agent.id,
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
            "display_phone_number": phone.get("display_phone_number"),
            "verified_name": phone.get("verified_name"),
            "connection_mode": connection_mode,
            "coexistence": connection_mode == "coexistence",
            "coexistence_ready": coexistence_ready,
            "echo_received": echo_received,
            "enabled": bool(channel.enabled),
            "runtime_ready": bool(channel.enabled and not blockers),
            "ready": not blockers,
            "blockers": blockers,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



@router.get("/settings")
def whatsapp_channel_settings(
    agent_id: int,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = _customer_agent(db, current_user, agent_id)
        _assert_whatsapp_selected_for_self_service(db, agent)
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "whatsapp",
            )
            .first()
        )
        if channel is None:
            raise HTTPException(404, "WhatsApp channel not found")
        config = reveal_config(channel.config) or {}
        return {
            "agent_id": agent.id,
            "channel_id": channel.id,
            "channel_type": "whatsapp",
            "settings": {
                "tone": str(config.get("tone") or "professional_friendly"),
                "response_style": str(config.get("response_style") or "conversational"),
                "response_length": str(config.get("response_length") or "concise"),
                "emoji_style": str(config.get("emoji_style") or "minimal"),
                "channel_instructions": str(config.get("channel_instructions") or ""),
            },
        }
    finally:
        db.close()


@router.put("/settings")
def update_whatsapp_channel_settings(
    data: CustomerWhatsAppSettingsUpdate,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = _customer_agent(db, current_user, data.agent_id)
        _assert_whatsapp_selected_for_self_service(db, agent)
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "whatsapp",
            )
            .with_for_update()
            .first()
        )
        if channel is None:
            raise HTTPException(404, "WhatsApp channel not found")
        current = reveal_config(channel.config) or {}
        updates = {
            "tone": str(data.tone or current.get("tone") or "professional_friendly").strip()[:80],
            "response_style": str(data.response_style or current.get("response_style") or "conversational").strip()[:80],
            "response_length": str(data.response_length or current.get("response_length") or "concise").strip()[:80],
            "emoji_style": str(data.emoji_style or current.get("emoji_style") or "minimal").strip()[:80],
            "channel_instructions": str(data.channel_instructions or "").strip()[:4000] or None,
        }
        channel.config = merge_config(channel.config, updates)
        audit_service.log(
            db=db,
            action="whatsapp.customer_settings_updated",
            resource_type="agent_channel",
            resource_id=channel.id,
            user_id=current_user.id,
            company_id=channel.company_id,
            details={
                "agent_id": agent.id,
                "behavior_fields": ["tone", "response_style", "response_length", "emoji_style", "channel_instructions"],
            },
        )
        db.commit()
        return {
            "status": "updated",
            "agent_id": agent.id,
            "channel_id": channel.id,
            "channel_type": "whatsapp",
            "settings": updates,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.post("/disconnect")
def disconnect_whatsapp(
    data: CustomerWhatsAppDisconnect,
    current_user: User = Depends(require_customer_manager),
):
    db = SessionLocal()
    try:
        agent = _customer_agent(db, current_user, data.agent_id)
        channel = (
            db.query(AgentChannel)
            .filter(
                AgentChannel.company_id == current_user.company_id,
                AgentChannel.agent_id == agent.id,
                AgentChannel.channel_type == "whatsapp",
            )
            .with_for_update()
            .first()
        )
        if channel is None:
            raise HTTPException(404, "WhatsApp channel not found")

        current = reveal_config(channel.config) or {}
        preserved = {
            key: current.get(key)
            for key in (
                "tone",
                "response_style",
                "response_length",
                "emoji_style",
                "channel_instructions",
            )
            if current.get(key) not in (None, "")
        }
        channel.enabled = False
        channel.config = preserved
        audit_service.log(
            db=db,
            action="whatsapp.customer_disconnected",
            resource_type="agent_channel",
            resource_id=channel.id,
            user_id=current_user.id,
            company_id=channel.company_id,
            details={"agent_id": agent.id},
        )
        db.commit()
        return {
            "status": "disconnected",
            "channel_id": channel.id,
            "channel_type": "whatsapp",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
