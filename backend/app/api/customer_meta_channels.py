from __future__ import annotations

from datetime import datetime
import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from backend.app.api.admin_channels import _activation_blockers, _ensure_channels_module
from backend.app.modules.channels.delivery import deactivate_managed_channel_route
from backend.app.api.admin_meta_whatsapp import _graph_request, _graph_url, _meta_settings
from backend.app.core.config.settings import settings
from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.core.database.connection import SessionLocal
from backend.app.core.dependencies import require_customer_manager
from backend.app.core.execution_claims import execution_claims
from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.models.user import User
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.catalog import get_channel_capability
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.instagram_oauth import (
    InstagramOAuthError,
    build_instagram_authorization_url,
    exchange_instagram_code,
    instagram_oauth_ready,
    issue_instagram_oauth_state,
    subscribe_instagram_messaging,
    verify_instagram_oauth_state,
)


router = APIRouter(
    prefix="/customer/meta/channels",
    tags=["Customer - Meta Channels"],
)

META_CUSTOMER_CHANNELS = {"instagram", "messenger"}
META_SCOPES = {
    "instagram": [
        "pages_show_list",
        "pages_manage_metadata",
        "instagram_basic",
        "instagram_manage_messages",
    ],
    "messenger": [
        "pages_show_list",
        "pages_manage_metadata",
        "pages_messaging",
    ],
}


class MetaDiscoverRequest(BaseModel):
    agent_id: int
    channel_type: str
    user_access_token: str


class MetaCompleteRequest(MetaDiscoverRequest):
    page_id: str


class MetaChannelAction(BaseModel):
    agent_id: int
    channel_type: str


class MetaChannelSettingsUpdate(MetaChannelAction):
    tone: str | None = None
    response_style: str | None = None
    response_length: str | None = None
    channel_instructions: str | None = None


class InstagramOAuthStart(BaseModel):
    agent_id: int


def _normalize_channel_type(value: str) -> str:
    channel_type = str(value or "").strip().lower()
    if channel_type not in META_CUSTOMER_CHANNELS:
        raise HTTPException(400, "Only Instagram DM and Facebook Messenger use this Meta connection flow")
    return channel_type


def _customer_channel(db, current_user: User, *, agent_id: int, channel_type: str):
    agent = (
        db.query(AIAgent)
        .filter(
            AIAgent.id == agent_id,
            AIAgent.company_id == current_user.company_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(404, "AI Employee not found")
    channel = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == current_user.company_id,
            AgentChannel.agent_id == agent.id,
            AgentChannel.channel_type == channel_type,
        )
        .first()
    )
    if channel is None:
        raise HTTPException(404, "This channel is not assigned to the AI Employee")
    capability = get_channel_capability(channel_type) or {}
    if capability.get("runtime_adapter") != "n8n_channel_gateway":
        raise HTTPException(409, "This channel is not available through Xvond Meta Connect")
    return agent, channel, capability


def _meta_connect_settings(channel_type: str) -> dict:
    config = _meta_settings()
    config["messenger_config_id"] = str(settings.META_MESSENGER_CONFIG_ID or "").strip()
    required = ["app_id", "app_secret"]
    if channel_type == "messenger":
        required.append("messenger_config_id")
    missing = [key for key in required if not config.get(key)]
    return {
        "ready": not missing and n8n_gateway.configured() and bool(settings.WORKFLOW_PUBLIC_URL),
        "missing": missing,
        **config,
    }


def _exchange_long_lived_user_token(user_access_token: str, config: dict) -> str:
    token = str(user_access_token or "").strip()
    if not token:
        raise HTTPException(400, "Meta authorization token is required")
    payload = _graph_request(
        "GET",
        _graph_url(
            config["graph_api_version"],
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": config["app_id"],
                "client_secret": config["app_secret"],
                "fb_exchange_token": token,
            },
        ),
    )
    exchanged = str(payload.get("access_token") or "").strip()
    if not exchanged:
        raise HTTPException(502, "Meta did not return a usable access token")
    return exchanged


def _page_assets(user_access_token: str, config: dict, channel_type: str) -> list[dict]:
    long_lived = _exchange_long_lived_user_token(user_access_token, config)
    payload = _graph_request(
        "GET",
        _graph_url(
            config["graph_api_version"],
            "me/accounts",
            {
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "limit": "100",
            },
        ),
        access_token=long_lived,
    )
    assets = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        page_id = str(row.get("id") or "").strip()
        page_token = str(row.get("access_token") or "").strip()
        if not page_id or not page_token:
            continue
        instagram = row.get("instagram_business_account") if isinstance(row.get("instagram_business_account"), dict) else None
        if channel_type == "instagram" and not instagram:
            continue
        assets.append(
            {
                "page_id": page_id,
                "page_name": str(row.get("name") or "").strip() or f"Page {page_id}",
                "page_access_token": page_token,
                "instagram_id": str((instagram or {}).get("id") or "").strip() or None,
                "instagram_username": str((instagram or {}).get("username") or "").strip() or None,
            }
        )
    return assets


def _public_asset(asset: dict, channel_type: str) -> dict:
    return {
        "page_id": asset["page_id"],
        "page_name": asset["page_name"],
        "instagram_id": asset.get("instagram_id") if channel_type == "instagram" else None,
        "instagram_username": asset.get("instagram_username") if channel_type == "instagram" else None,
        "label": (
            f"@{asset.get('instagram_username')}"
            if channel_type == "instagram" and asset.get("instagram_username")
            else asset["page_name"]
        ),
    }


def _subscribe_asset(*, sender_id: str, access_token: str, config: dict) -> None:
    payload = _graph_request(
        "POST",
        _graph_url(
            config["graph_api_version"],
            f"{sender_id}/subscribed_apps",
        ),
        access_token=access_token,
        form={"subscribed_fields": "messages,messaging_postbacks"},
    )
    if payload.get("success") is not True:
        raise HTTPException(502, "Meta did not confirm messaging webhook subscription")


def _instagram_portal_redirect(status: str) -> str:
    import urllib.parse

    configured = str(settings.META_INSTAGRAM_REDIRECT_URI or "").strip()
    parsed = urllib.parse.urlparse(configured) if configured else None
    if parsed and parsed.scheme and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
    else:
        origin = str(settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    suffix = f"/customer-ui?instagram_oauth={status}#channels"
    return f"{origin.rstrip('/')}{suffix}" if origin else suffix


def _auto_activate_connected_channel(db, channel: AgentChannel) -> list[str]:
    """Turn a successfully connected managed Meta channel live when it is ready."""
    channel.enabled = False
    _ensure_channels_module(db, channel.company_id)
    db.flush()
    blockers = _activation_blockers(db, channel)
    if not blockers:
        channel.enabled = True
    return blockers


def _instagram_manager_from_state(db, payload: dict) -> User:
    user = db.query(User).filter(User.id == int(payload.get("user_id") or 0)).first()
    company_id = int(payload.get("company_id") or 0)
    if (
        user is None
        or not user.active
        or user.role not in {"owner", "admin", "manager"}
        or int(user.company_id or 0) != company_id
    ):
        raise HTTPException(403, "Instagram connection session is no longer authorized")
    return user


def _provision_direct_instagram(
    db,
    *,
    user: User,
    agent_id: int,
    sender_id: str,
    username: str,
    access_token: str,
) -> None:
    agent, channel, capability = _customer_channel(
        db,
        user,
        agent_id=agent_id,
        channel_type="instagram",
    )
    channel = (
        db.query(AgentChannel)
        .filter(AgentChannel.id == channel.id)
        .with_for_update()
        .first()
    )
    if channel.enabled:
        raise HTTPException(409, "Deactivate this channel before changing its Instagram connection")

    current = reveal_config(channel.config) or {}
    connection_key = str(current.get("connection_key") or "").strip() or secrets.token_urlsafe(24)
    provider_secret = secrets.token_urlsafe(32)
    provider_setup = capability.get("provider_setup") or {}
    provider_path = str(provider_setup.get("provider_path") or "").strip()
    if not provider_path:
        raise HTTPException(503, "Xvond Instagram provider route is unavailable")

    label = f"@{username}" if username else f"Instagram {sender_id}"
    provider_config = {
        "sender_id": sender_id,
        "access_token": access_token,
        "app_secret": settings.META_INSTAGRAM_APP_SECRET,
        "graph_version": settings.META_GRAPH_API_VERSION,
    }
    try:
        provisioned = n8n_gateway.provision_channel(
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            channel_id=channel.id,
            channel_type="instagram",
            connection_key=connection_key,
            provider_type="meta",
            provider_url=settings.WORKFLOW_PUBLIC_URL + provider_path,
            provider_secret=provider_secret,
            provider_config=provider_config,
            provider_account_label=label,
        )
    except N8NGatewayError as exc:
        raise HTTPException(502, "Xvond could not provision the Instagram channel") from exc
    if provisioned.get("success") is not True:
        raise HTTPException(502, "Xvond could not provision the Instagram channel")

    try:
        verified = n8n_gateway.execute(
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            action="channel.check",
            data={
                "channel_id": channel.id,
                "channel_type": "instagram",
                "connection_key": connection_key,
            },
        )
    except N8NGatewayError as exc:
        raise HTTPException(502, "Xvond could not verify the Instagram channel") from exc
    verified_data = verified.get("data") if isinstance(verified.get("data"), dict) else {}
    if verified.get("success") is not True or verified_data.get("configured") is not True:
        raise HTTPException(409, "Instagram authorization succeeded but the Xvond route is not ready")

    inbound_path = str(provider_setup.get("inbound_path") or "").strip()
    channel.config = merge_config(
        channel.config,
        {
            "connection_key": connection_key,
            "provisioning_state": "connected",
            "provisioning_error": None,
            "registry_cleanup_state": "active",
            "connection_method": "instagram_direct_oauth",
            "provider_account_label": label,
            "provider_inbound_url": (
                settings.WORKFLOW_PUBLIC_URL + inbound_path if inbound_path else None
            ),
            "meta_page_id": None,
            "meta_sender_id": sender_id,
            "meta_connected_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        },
    )
    blockers = _auto_activate_connected_channel(db, channel)
    audit_service.log(
        db=db,
        action="channel.instagram_direct_connected",
        resource_type="channel",
        resource_id=channel.id,
        user_id=user.id,
        company_id=channel.company_id,
        details={
            "agent_id": agent.id,
            "channel_type": "instagram",
            "provider_account_label": label,
            "connection_method": "instagram_direct_oauth",
            "auto_activated": bool(channel.enabled),
            "activation_blockers": blockers,
        },
    )


@router.post("/instagram/oauth/start")
def instagram_oauth_start(
    data: InstagramOAuthStart,
    current_user: User = Depends(require_customer_manager),
):
    if not instagram_oauth_ready() or not n8n_gateway.configured() or not settings.WORKFLOW_PUBLIC_URL:
        raise HTTPException(503, "Direct Instagram Login is not configured")
    db = SessionLocal()
    try:
        _agent, channel, _capability = _customer_channel(
            db,
            current_user,
            agent_id=data.agent_id,
            channel_type="instagram",
        )
        if channel.enabled:
            raise HTTPException(409, "Deactivate this channel before changing its Instagram connection")
        state = issue_instagram_oauth_state(
            user_id=current_user.id,
            company_id=current_user.company_id,
            agent_id=data.agent_id,
        )
        return {
            "authorization_url": build_instagram_authorization_url(state=state),
            "expires_in": 600,
        }
    finally:
        db.close()


@router.get("/instagram/oauth/callback")
def instagram_oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
):
    try:
        oauth_state = verify_instagram_oauth_state(state)
    except InstagramOAuthError as exc:
        raise HTTPException(400, str(exc)) from exc

    if error:
        return RedirectResponse(_instagram_portal_redirect("cancelled"), status_code=303)
    if not str(code or "").strip():
        raise HTTPException(400, "Instagram authorization code is missing")

    nonce = str(oauth_state.get("nonce") or "").strip()
    if not execution_claims.claim(f"instagram_oauth:{nonce}", ttl_seconds=900):
        raise HTTPException(409, "Instagram connection callback was already used")

    try:
        token = exchange_instagram_code(code=str(code).strip())
        subscribe_instagram_messaging(
            user_id=token["user_id"],
            access_token=token["access_token"],
        )
    except InstagramOAuthError as exc:
        raise HTTPException(502, str(exc)) from exc

    db = SessionLocal()
    try:
        user = _instagram_manager_from_state(db, oauth_state)
        _provision_direct_instagram(
            db,
            user=user,
            agent_id=int(oauth_state["agent_id"]),
            sender_id=str(token["user_id"]),
            username=str(token.get("username") or ""),
            access_token=str(token["access_token"]),
        )
        db.commit()
        return RedirectResponse(_instagram_portal_redirect("connected"), status_code=303)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/connect/config")
def connect_config(
    agent_id: int,
    channel_type: str,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(channel_type)
    db = SessionLocal()
    try:
        _agent, channel, _capability = _customer_channel(
            db,
            current_user,
            agent_id=agent_id,
            channel_type=channel_type,
        )
        config = _meta_connect_settings(channel_type)
        return {
            "ready": config["ready"],
            "agent_id": agent_id,
            "channel_id": channel.id,
            "channel_type": channel_type,
            "app_id": config["app_id"] if config["ready"] else None,
            "graph_api_version": config["graph_api_version"],
            "scopes": META_SCOPES[channel_type],
            "config_id": config["messenger_config_id"] if channel_type == "messenger" else None,
            "missing_settings": config["missing"],
            "connected": str((reveal_config(channel.config) or {}).get("provisioning_state") or "").lower() == "connected",
        }
    finally:
        db.close()


@router.post("/connect/discover")
def discover_assets(
    data: MetaDiscoverRequest,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(data.channel_type)
    config = _meta_connect_settings(channel_type)
    if not config["ready"]:
        raise HTTPException(503, "Xvond Meta Connect is not configured")
    db = SessionLocal()
    try:
        _customer_channel(
            db,
            current_user,
            agent_id=data.agent_id,
            channel_type=channel_type,
        )
    finally:
        db.close()
    assets = _page_assets(data.user_access_token, config, channel_type)
    return {
        "channel_type": channel_type,
        "assets": [_public_asset(item, channel_type) for item in assets],
    }


@router.post("/connect/complete")
def complete_connect(
    data: MetaCompleteRequest,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(data.channel_type)
    config = _meta_connect_settings(channel_type)
    if not config["ready"]:
        raise HTTPException(503, "Xvond Meta Connect is not configured")

    assets = _page_assets(data.user_access_token, config, channel_type)
    selected = next((item for item in assets if item["page_id"] == str(data.page_id).strip()), None)
    if selected is None:
        raise HTTPException(400, "The selected Meta asset is not available to this account")

    sender_id = (
        str(selected.get("instagram_id") or "").strip()
        if channel_type == "instagram"
        else selected["page_id"]
    )
    if not sender_id:
        raise HTTPException(400, "The selected Facebook Page has no professional Instagram account")

    _subscribe_asset(
        sender_id=sender_id,
        access_token=selected["page_access_token"],
        config=config,
    )

    db = SessionLocal()
    try:
        agent, channel, capability = _customer_channel(
            db,
            current_user,
            agent_id=data.agent_id,
            channel_type=channel_type,
        )
        channel = (
            db.query(AgentChannel)
            .filter(AgentChannel.id == channel.id)
            .with_for_update()
            .first()
        )
        if channel.enabled:
            raise HTTPException(409, "Deactivate this channel before changing its Meta connection")

        current = reveal_config(channel.config) or {}
        connection_key = str(current.get("connection_key") or "").strip() or secrets.token_urlsafe(24)
        provider_secret = secrets.token_urlsafe(32)
        provider_setup = capability.get("provider_setup") or {}
        provider_path = str(provider_setup.get("provider_path") or "").strip()
        if not provider_path:
            raise HTTPException(503, "Xvond Meta provider route is unavailable")

        label = (
            f"@{selected.get('instagram_username')}"
            if channel_type == "instagram" and selected.get("instagram_username")
            else selected["page_name"]
        )
        provider_config = {
            "sender_id": sender_id,
            "access_token": selected["page_access_token"],
            "app_secret": config["app_secret"],
            "graph_version": config["graph_api_version"],
        }
        try:
            provisioned = n8n_gateway.provision_channel(
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                channel_id=channel.id,
                channel_type=channel_type,
                connection_key=connection_key,
                provider_type="meta",
                provider_url=settings.WORKFLOW_PUBLIC_URL + provider_path,
                provider_secret=provider_secret,
                provider_config=provider_config,
                provider_account_label=label,
            )
        except N8NGatewayError as exc:
            raise HTTPException(502, "Xvond could not provision the Meta channel") from exc
        if provisioned.get("success") is not True:
            raise HTTPException(502, "Xvond could not provision the Meta channel")

        try:
            verified = n8n_gateway.execute(
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                action="channel.check",
                data={
                    "channel_id": channel.id,
                    "channel_type": channel_type,
                    "connection_key": connection_key,
                },
            )
        except N8NGatewayError as exc:
            raise HTTPException(502, "Xvond could not verify the Meta channel") from exc
        verified_data = verified.get("data") if isinstance(verified.get("data"), dict) else {}
        if verified.get("success") is not True or verified_data.get("configured") is not True:
            raise HTTPException(409, "Meta connection was authorized but the Xvond route is not ready")

        inbound_path = str(provider_setup.get("inbound_path") or "").strip()
        channel.config = merge_config(
            channel.config,
            {
                "connection_key": connection_key,
                "provisioning_state": "connected",
                "provisioning_error": None,
                "registry_cleanup_state": "active",
                "connection_method": "meta_customer_connect",
                "provider_account_label": label,
                "provider_inbound_url": (
                    settings.WORKFLOW_PUBLIC_URL + inbound_path if inbound_path else None
                ),
                "meta_page_id": selected["page_id"],
                "meta_sender_id": sender_id,
                "meta_connected_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            },
        )
        blockers = _auto_activate_connected_channel(db, channel)
        audit_service.log(
            db=db,
            action="channel.meta_customer_connected",
            resource_type="channel",
            resource_id=channel.id,
            user_id=current_user.id,
            company_id=channel.company_id,
            details={
                "agent_id": agent.id,
                "channel_type": channel_type,
                "provider_account_label": label,
                "connection_method": "meta_customer_connect",
                "auto_activated": bool(channel.enabled),
                "activation_blockers": blockers,
            },
        )
        db.commit()
        return {
            "status": "live" if channel.enabled else "connected",
            "channel_id": channel.id,
            "channel_type": channel_type,
            "account_label": label,
            "enabled": bool(channel.enabled),
            "ready_for_launch": bool(channel.enabled),
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


@router.post("/health")
def channel_health(
    data: MetaChannelAction,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(data.channel_type)
    db = SessionLocal()
    try:
        _agent, channel, _capability = _customer_channel(
            db,
            current_user,
            agent_id=data.agent_id,
            channel_type=channel_type,
        )
        config = reveal_config(channel.config) or {}
        state = str(config.get("provisioning_state") or "").strip().lower()
        connection_key = str(config.get("connection_key") or "").strip()
        blockers = _activation_blockers(db, channel)
        if state != "connected" or not connection_key:
            return {
                "healthy": False,
                "connected": False,
                "enabled": bool(channel.enabled),
                "channel_type": channel_type,
                "account_label": config.get("provider_account_label"),
                "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "issue": "Channel is not connected",
                "blockers": blockers,
            }
        if not n8n_gateway.configured():
            return {
                "healthy": False,
                "connected": True,
                "enabled": bool(channel.enabled),
                "channel_type": channel_type,
                "account_label": config.get("provider_account_label"),
                "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "issue": "Xvond channel gateway is unavailable",
                "blockers": blockers,
            }
        try:
            result = n8n_gateway.execute(
                company_id=channel.company_id,
                agent_id=channel.agent_id,
                action="channel.check",
                data={
                    "channel_id": channel.id,
                    "channel_type": channel_type,
                    "connection_key": connection_key,
                },
            )
        except N8NGatewayError:
            result = {"success": False, "data": {}}
        result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        healthy = result.get("success") is True and result_data.get("configured") is True
        return {
            "healthy": healthy,
            "connected": True,
            "enabled": bool(channel.enabled),
            "channel_type": channel_type,
            "account_label": config.get("provider_account_label"),
            "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "issue": None if healthy else "Xvond could not verify the provider route",
            "blockers": blockers,
        }
    finally:
        db.close()



@router.get("/settings")
def meta_channel_settings(
    agent_id: int,
    channel_type: str,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(channel_type)
    db = SessionLocal()
    try:
        _agent, channel, _capability = _customer_channel(
            db,
            current_user,
            agent_id=agent_id,
            channel_type=channel_type,
        )
        config = reveal_config(channel.config) or {}
        return {
            "agent_id": agent_id,
            "channel_id": channel.id,
            "channel_type": channel_type,
            "settings": {
                "tone": str(config.get("tone") or "professional_friendly"),
                "response_style": str(config.get("response_style") or "conversational"),
                "response_length": str(config.get("response_length") or "concise"),
                "channel_instructions": str(config.get("channel_instructions") or ""),
            },
        }
    finally:
        db.close()


@router.put("/settings")
def update_meta_channel_settings(
    data: MetaChannelSettingsUpdate,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(data.channel_type)
    db = SessionLocal()
    try:
        agent, channel, _capability = _customer_channel(
            db,
            current_user,
            agent_id=data.agent_id,
            channel_type=channel_type,
        )
        channel = (
            db.query(AgentChannel)
            .filter(AgentChannel.id == channel.id)
            .with_for_update()
            .first()
        )
        current = reveal_config(channel.config) or {}
        updates = {
            "tone": str(data.tone or current.get("tone") or "professional_friendly").strip()[:80],
            "response_style": str(data.response_style or current.get("response_style") or "conversational").strip()[:80],
            "response_length": str(data.response_length or current.get("response_length") or "concise").strip()[:80],
            "channel_instructions": str(data.channel_instructions or "").strip()[:4000] or None,
        }
        channel.config = merge_config(channel.config, updates)
        audit_service.log(
            db=db,
            action="channel.meta_customer_settings_updated",
            resource_type="channel",
            resource_id=channel.id,
            user_id=current_user.id,
            company_id=channel.company_id,
            details={
                "agent_id": agent.id,
                "channel_type": channel_type,
                "behavior_fields": ["tone", "response_style", "response_length", "channel_instructions"],
            },
        )
        db.commit()
        return {
            "status": "updated",
            "agent_id": agent.id,
            "channel_id": channel.id,
            "channel_type": channel_type,
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
def disconnect_channel(
    data: MetaChannelAction,
    current_user: User = Depends(require_customer_manager),
):
    channel_type = _normalize_channel_type(data.channel_type)
    db = SessionLocal()
    try:
        agent, channel, _capability = _customer_channel(
            db,
            current_user,
            agent_id=data.agent_id,
            channel_type=channel_type,
        )
        channel = (
            db.query(AgentChannel)
            .filter(AgentChannel.id == channel.id)
            .with_for_update()
            .first()
        )
        current = reveal_config(channel.config) or {}
        channel.enabled = False
        cleanup = deactivate_managed_channel_route(channel)
        if cleanup.get("required") is True and cleanup.get("complete") is not True:
            raise HTTPException(
                502,
                "Xvond could not safely remove the provider route; the channel was not disconnected",
            )

        preserved = {
            "provisioning_state": "cancelled",
            "registry_cleanup_state": "complete",
            "request_source": current.get("request_source"),
            "tone": current.get("tone"),
            "response_style": current.get("response_style"),
            "response_length": current.get("response_length"),
            "channel_instructions": current.get("channel_instructions"),
            "connection_method": None,
            "provider_account_label": None,
            "provider_inbound_url": None,
            "meta_page_id": None,
            "meta_sender_id": None,
            "meta_connected_at": None,
        }
        channel.config = preserved
        audit_service.log(
            db=db,
            action="channel.meta_customer_disconnected",
            resource_type="channel",
            resource_id=channel.id,
            user_id=current_user.id,
            company_id=channel.company_id,
            details={
                "agent_id": agent.id,
                "channel_type": channel_type,
                "managed_route_cleanup": cleanup.get("reason"),
            },
        )
        db.commit()
        return {
            "status": "disconnected",
            "channel_id": channel.id,
            "channel_type": channel_type,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
