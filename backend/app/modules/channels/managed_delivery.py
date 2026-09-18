from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from backend.app.core.config_secrets import reveal_config
from backend.app.core.n8n_gateway import N8NGatewayError, n8n_gateway
from backend.app.modules.ai_agent.models import AIMessage
from backend.app.modules.channels.acceptance import mark_customer_roundtrip
from backend.app.modules.channels.catalog import (
    N8N_CHANNEL_ADAPTER,
    canonical_channel_type,
    get_channel_capability,
)
from backend.app.modules.channels.models import (
    AgentChannel,
    ManagedChannelOutboundDelivery,
)


SUCCESS_STATES = {"accepted"}
FINAL_STATES = SUCCESS_STATES | {"unknown"}


def _now() -> datetime:
    return datetime.utcnow()


def delivery_payload(row: ManagedChannelOutboundDelivery) -> dict:
    return {
        "delivery_id": row.id,
        "status": row.status,
        "retryable": bool(row.retryable),
        "attempts": int(row.attempts or 0),
        "provider_message_id": row.provider_message_id,
        "error_code": row.last_error_code,
    }


def ensure_delivery(
    db,
    *,
    idempotency_key: str,
    company_id: int,
    agent_id: int,
    conversation_id: int,
    channel_id: int,
    message_id: int,
    external_contact_id: str,
    inbound_external_message_id: str | None = None,
) -> ManagedChannelOutboundDelivery:
    key = str(idempotency_key or "").strip()
    contact = str(external_contact_id or "").strip()
    if not key:
        raise ValueError("Managed channel delivery idempotency key is required")
    if not contact:
        raise ValueError("Managed channel delivery external contact is required")

    existing = (
        db.query(ManagedChannelOutboundDelivery)
        .filter(ManagedChannelOutboundDelivery.idempotency_key == key)
        .first()
    )
    if existing is not None:
        expected = (
            int(company_id),
            int(agent_id),
            int(conversation_id),
            int(channel_id),
            int(message_id),
            contact,
        )
        actual = (
            existing.company_id,
            existing.agent_id,
            existing.conversation_id,
            existing.channel_id,
            existing.message_id,
            existing.external_contact_id,
        )
        if actual != expected:
            raise RuntimeError("Managed channel delivery idempotency scope conflict")
        return existing

    row = ManagedChannelOutboundDelivery(
        idempotency_key=key,
        company_id=company_id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        channel_id=channel_id,
        message_id=message_id,
        external_contact_id=contact,
        inbound_external_message_id=(
            str(inbound_external_message_id or "").strip() or None
        ),
        status="pending",
        retryable=False,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        row = (
            db.query(ManagedChannelOutboundDelivery)
            .filter(ManagedChannelOutboundDelivery.idempotency_key == key)
            .first()
        )
        if row is None:
            raise
    return row


def _locked(db, delivery_id: int) -> ManagedChannelOutboundDelivery:
    row = (
        db.query(ManagedChannelOutboundDelivery)
        .filter(ManagedChannelOutboundDelivery.id == delivery_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise RuntimeError("Managed channel delivery record disappeared")
    return row


def attempt_delivery(db, *, delivery_id: int) -> dict:
    """Perform one durable managed-channel send without blind network retries."""

    row = _locked(db, delivery_id)
    if row.status in SUCCESS_STATES:
        return {"success": True, "already_sent": True, **delivery_payload(row)}
    if row.status == "unknown":
        return {"success": False, "unknown": True, **delivery_payload(row)}
    if row.status == "sending":
        row.status = "unknown"
        row.retryable = False
        row.last_error_code = "interrupted_after_send_started"
        row.updated_at = _now()
        db.commit()
        return {"success": False, "unknown": True, **delivery_payload(row)}
    if row.status == "failed" and not row.retryable:
        return {"success": False, "permanent": True, **delivery_payload(row)}

    message = db.query(AIMessage).filter(AIMessage.id == row.message_id).first()
    channel = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.id == row.channel_id,
            AgentChannel.company_id == row.company_id,
            AgentChannel.agent_id == row.agent_id,
            AgentChannel.enabled.is_(True),
        )
        .first()
    )
    if message is None or message.conversation_id != row.conversation_id:
        row.status = "failed"
        row.retryable = False
        row.last_error_code = "message_missing"
        row.failed_at = _now()
        db.commit()
        return {"success": False, "permanent": True, **delivery_payload(row)}
    if channel is None:
        row.status = "failed"
        row.retryable = False
        row.last_error_code = "channel_unavailable"
        row.failed_at = _now()
        db.commit()
        return {"success": False, "permanent": True, **delivery_payload(row)}

    capability = get_channel_capability(channel.channel_type) or {}
    config = reveal_config(channel.config) or {}
    connection_key = str(config.get("connection_key") or "").strip()
    if (
        capability.get("runtime_adapter") != N8N_CHANNEL_ADAPTER
        or str(config.get("provisioning_state") or "").strip().lower() != "connected"
        or not connection_key
        or not n8n_gateway.configured()
    ):
        row.status = "failed"
        row.retryable = False
        row.last_error_code = "managed_channel_not_ready"
        row.failed_at = _now()
        db.commit()
        return {"success": False, "permanent": True, **delivery_payload(row)}

    row.status = "sending"
    row.retryable = False
    row.attempts = int(row.attempts or 0) + 1
    row.last_error_code = None
    row.updated_at = _now()
    db.commit()

    try:
        result = n8n_gateway.execute(
            company_id=row.company_id,
            agent_id=row.agent_id,
            conversation_id=row.conversation_id,
            action="channel.send",
            request_id=row.idempotency_key,
            max_retries_override=0,
            data={
                "channel_id": row.channel_id,
                "channel_type": canonical_channel_type(channel.channel_type),
                "connection_key": connection_key,
                "external_contact_id": row.external_contact_id,
                "message": message.content,
                "idempotency_key": row.idempotency_key,
            },
        )
    except N8NGatewayError:
        row = _locked(db, delivery_id)
        row.status = "unknown"
        row.retryable = False
        row.last_error_code = "network_outcome_unknown"
        row.updated_at = _now()
        db.commit()
        return {"success": False, "unknown": True, **delivery_payload(row)}

    row = _locked(db, delivery_id)
    if result.get("success") is True:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        provider_message_id = str((data or {}).get("provider_message_id") or "").strip()
        if not provider_message_id:
            row.status = "unknown"
            row.retryable = False
            row.last_error_code = "provider_message_id_missing"
            row.updated_at = _now()
            db.commit()
            return {"success": False, "unknown": True, **delivery_payload(row)}

        row.provider_message_id = provider_message_id
        row.status = "accepted"
        row.retryable = False
        row.last_error_code = None
        row.accepted_at = _now()
        mark_customer_roundtrip(
            channel,
            source=f"{canonical_channel_type(channel.channel_type)}_provider_confirmed",
        )
        db.commit()
        return {"success": True, **delivery_payload(row)}

    row.status = "failed"
    row.retryable = False
    row.last_error_code = str(result.get("error_code") or "provider_rejected")[:160]
    row.failed_at = _now()
    db.commit()
    return {"success": False, "permanent": True, **delivery_payload(row)}
