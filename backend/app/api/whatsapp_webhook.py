import hashlib
import hmac
import json
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from backend.app.api.admin_meta_whatsapp import _meta_settings
from backend.app.core.agent_runtime import agent_runtime
from backend.app.core.config_secrets import merge_config, reveal_config
from backend.app.core.customer_runtime_policy import (
    human_handoff_acknowledgement,
    is_service_access_error,
    safe_service_unavailable_message,
)
from backend.app.core.database.connection import SessionLocal
from backend.app.models.company_module import CompanyModule
from backend.app.modules.ai_agent.models import AIMessage
from backend.app.modules.audit.service import audit_service
from backend.app.modules.channels.conversation_source import bind_conversation_source
from backend.app.modules.channels.handoff import (
    activate_human_handoff,
    echo_recipient,
    extend_human_handoff,
    human_handoff_active,
    normalize_message,
    requests_human,
    resume_ai,
)
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.channels.whatsapp_delivery import (
    apply_provider_status,
    attempt_delivery,
    ensure_delivery,
    retry_delivery_for_inbound,
)
from backend.app.modules.channels.whatsapp_models import (
    WhatsAppInboundMessage,
    WhatsAppSession,
)
from backend.app.modules.channels.whatsapp_queue import whatsapp_job_queue
from backend.app.modules.tools.business_models import HumanHandoff

router = APIRouter(prefix="/webhooks/whatsapp", tags=["WhatsApp Webhook"])
ACTIVE_HANDOFF_STATUSES = ["pending", "in_progress"]
TERMINAL_INBOUND_STATUSES = {"processed", "ignored"}
RETURN_TO_AI_COMMANDS = {
    "/ai",
    "/resume-ai",
    "/resume_ai",
    "رجع ai",
    "ارجع ai",
    "رجع الذكاء الاصطناعي",
    "ارجع الذكاء الاصطناعي",
}
logger = logging.getLogger(__name__)


def get_whatsapp_channels(db):
    return (
        db.query(AgentChannel)
        .join(CompanyModule, CompanyModule.company_id == AgentChannel.company_id)
        .filter(
            AgentChannel.channel_type == "whatsapp",
            CompanyModule.module_name == "channels",
            CompanyModule.enabled.is_(True),
        )
        .all()
    )


def find_channel_by_phone_number_id(db, phone_number_id: str):
    return next(
        (
            channel
            for channel in get_whatsapp_channels(db)
            if str(reveal_config(channel.config).get("phone_number_id", ""))
            == str(phone_number_id)
        ),
        None,
    )


def verify_signature(raw_body: bytes, signature: str | None, app_secret: str | None):
    if not app_secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def claim_message(
    db,
    message_id: str,
    company_id: int,
    agent_id: int,
    wa_id: str,
) -> bool:
    """Claim or resume a signed inbound event.

    ``processing`` is resumable because a business action can intentionally
    commit durable side-effect state inside the turn. ``processed``/``ignored``
    are terminal. Scope mismatches are never treated as benign duplicates.
    """
    item = WhatsAppInboundMessage(
        external_message_id=message_id,
        company_id=company_id,
        agent_id=agent_id,
        wa_id=wa_id,
        status="processing",
        attempts=1,
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
        return True
    except IntegrityError:
        existing = (
            db.query(WhatsAppInboundMessage)
            .filter(WhatsAppInboundMessage.external_message_id == message_id)
            .with_for_update()
            .first()
        )
        if existing is None:
            raise
        if (
            existing.company_id != company_id
            or existing.agent_id != agent_id
            or existing.wa_id != wa_id
        ):
            raise RuntimeError("WhatsApp inbound message identity scope conflict")
        if existing.status in TERMINAL_INBOUND_STATUSES:
            return False
        existing.status = "processing"
        existing.attempts = int(existing.attempts or 0) + 1
        existing.updated_at = datetime.utcnow()
        db.flush()
        return True


def complete_message_claim(db, message_id: str, *, status: str = "processed") -> None:
    if status not in TERMINAL_INBOUND_STATUSES:
        raise ValueError("Invalid terminal WhatsApp inbound status")
    row = (
        db.query(WhatsAppInboundMessage)
        .filter(WhatsAppInboundMessage.external_message_id == message_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise RuntimeError("WhatsApp inbound claim is missing")
    row.status = status
    row.updated_at = datetime.utcnow()
    db.flush()


def release_message_claim(db, message_id: str):
    # Roll back only the current transaction. If a business action already made
    # the processing row durable, the next queue attempt will resume it.
    db.rollback()


def lock_contact(db, agent_id: int, wa_id: str):
    key = f"xvond-whatsapp:{agent_id}:{wa_id}"
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": key},
        )


def _ensure_handoff_record(
    db,
    *,
    channel: AgentChannel,
    conversation_id: int,
    reason: str,
    status: str = "pending",
):
    row = (
        db.query(HumanHandoff)
        .filter(
            HumanHandoff.company_id == channel.company_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
        )
        .order_by(HumanHandoff.id.desc())
        .first()
    )
    if row is None:
        row = HumanHandoff(
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            conversation_id=conversation_id,
            reason=reason,
            priority=(
                "high" if reason == "service_limit_or_entitlement" else "normal"
            ),
            department="customer_service",
            status=status,
        )
        db.add(row)
    else:
        row.reason = reason or row.reason
        if row.status == "pending" and status == "in_progress":
            row.status = "in_progress"
    return row


def _is_return_to_ai_command(content: str) -> bool:
    return normalize_message(content) in RETURN_TO_AI_COMMANDS


def _complete_active_handoffs(db, *, company_id: int, conversation_id: int, now: datetime):
    rows = (
        db.query(HumanHandoff)
        .filter(
            HumanHandoff.company_id == company_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
        )
        .all()
    )
    for row in rows:
        row.status = "completed"
        row.completed_at = now
        row.updated_at = now
    return len(rows)


def _business_app_echo_content(echo: dict) -> str:
    message_type = str(echo.get("type") or "unknown").strip().lower()
    payload = echo.get(message_type) or {}
    if message_type == "text":
        body = str((echo.get("text") or {}).get("body") or "").strip()
        return body or "[Empty WhatsApp Business message]"
    if isinstance(payload, dict):
        caption = str(payload.get("caption") or "").strip()
        if caption:
            return caption
    labels = {
        "image": "[Image sent from WhatsApp Business]",
        "video": "[Video sent from WhatsApp Business]",
        "audio": "[Audio sent from WhatsApp Business]",
        "voice": "[Voice message sent from WhatsApp Business]",
        "document": "[Document sent from WhatsApp Business]",
        "sticker": "[Sticker sent from WhatsApp Business]",
        "location": "[Location sent from WhatsApp Business]",
        "contacts": "[Contact shared from WhatsApp Business]",
        "contact": "[Contact shared from WhatsApp Business]",
        "reaction": "[Reaction sent from WhatsApp Business]",
        "edit": "[Message edited in WhatsApp Business]",
        "revoke": "[Message deleted in WhatsApp Business]",
    }
    return labels.get(
        message_type,
        f"[{message_type or 'Message'} sent from WhatsApp Business]",
    )


def _business_app_echo_created_at(echo: dict) -> datetime | None:
    timestamp = str(echo.get("timestamp") or "").strip()
    if not timestamp:
        return None
    try:
        return datetime.utcfromtimestamp(int(timestamp))
    except (TypeError, ValueError, OverflowError):
        return None


def _echo_precedes_explicit_ai_resume(
    session: WhatsAppSession,
    *,
    message_id: str,
    created_at: datetime | None,
) -> bool:
    resumed_at = session.ai_resumed_at
    if resumed_at is None:
        return False
    if session.ai_resume_echo_id and session.ai_resume_echo_id == message_id:
        return True
    if created_at is not None and created_at < resumed_at.replace(microsecond=0):
        return True
    return False


def process_business_app_echo(
    db,
    channel: AgentChannel,
    value: dict,
) -> list[dict]:
    processed = []
    phone_number_id = str(
        (value.get("metadata") or {}).get("phone_number_id") or ""
    )
    for echo in value.get("message_echoes", []) or []:
        wa_id = echo_recipient(echo)
        message_id = str(echo.get("id") or "").strip()
        if not wa_id or not message_id:
            processed.append(
                {"message_id": message_id, "status": "ignored_invalid_echo"}
            )
            continue
        if not claim_message(
            db=db,
            message_id=message_id,
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            wa_id=wa_id,
        ):
            processed.append({"message_id": message_id, "status": "duplicate"})
            continue

        content = _business_app_echo_content(echo)
        try:
            lock_contact(db, channel.agent_id, wa_id)
            session = _get_or_create_whatsapp_session(
                db=db,
                channel=channel,
                wa_id=wa_id,
                phone_number_id=phone_number_id,
                incoming_text=content,
            )
            created_at = _business_app_echo_created_at(echo)

            config = reveal_config(channel.config) or {}
            if config.get("coexistence") is True:
                channel.config = merge_config(
                    channel.config,
                    {
                        "coexistence_echo_received_at": datetime.utcnow().isoformat()
                    },
                )
                if config.get("activation_pending_coexistence"):
                    from backend.app.api.admin_channels import _activation_blockers

                    if not _activation_blockers(db, channel):
                        channel.enabled = True
                        channel.config = merge_config(
                            channel.config,
                            {"activation_pending_coexistence": False},
                        )

            if _is_return_to_ai_command(content):
                marker = whatsapp_job_queue.human_marker(phone_number_id, wa_id)
                resumed_at = created_at or datetime.utcnow()
                completed_handoffs = _complete_active_handoffs(
                    db,
                    company_id=channel.company_id,
                    conversation_id=session.conversation_id,
                    now=resumed_at,
                )
                resume_ai(session, now=resumed_at)
                audit_service.log(
                    db=db,
                    company_id=channel.company_id,
                    action="whatsapp.ai_resumed_by_business_app_command",
                    resource_type="channel",
                    resource_id=channel.id,
                    details={
                        "message_id": message_id,
                        "conversation_id": session.conversation_id,
                        "wa_id": wa_id,
                        "source": "whatsapp_business_app",
                        "completed_handoffs": completed_handoffs,
                    },
                )
                complete_message_claim(db, message_id)
                db.commit()
                whatsapp_job_queue.clear_human_marker(
                    phone_number_id,
                    wa_id,
                    marker,
                )
                processed.append(
                    {
                        "message_id": message_id,
                        "conversation_id": session.conversation_id,
                        "status": "ai_resumed_by_command",
                        "mirrored": False,
                    }
                )
                continue

            stale_after_resume = _echo_precedes_explicit_ai_resume(
                session,
                message_id=message_id,
                created_at=created_at,
            )
            if not stale_after_resume:
                activate_human_handoff(
                    session,
                    reason="business_app_reply",
                    now=created_at,
                    human_message=True,
                )
                _ensure_handoff_record(
                    db,
                    channel=channel,
                    conversation_id=session.conversation_id,
                    reason="business_app_reply",
                    status="in_progress",
                )
            elif session.ai_resume_echo_id == message_id:
                session.ai_resume_echo_id = None

            source_key = f"whatsapp-echo:{message_id}"
            mirrored = (
                db.query(AIMessage)
                .filter(AIMessage.source_key == source_key)
                .first()
            )
            if mirrored is None:
                message_kwargs = {
                    "conversation_id": session.conversation_id,
                    "role": "human",
                    "content": content,
                    "source_key": source_key,
                }
                if created_at is not None:
                    message_kwargs["created_at"] = created_at
                db.add(AIMessage(**message_kwargs))

            audit_service.log(
                db=db,
                company_id=channel.company_id,
                action="whatsapp.human_reply_detected",
                resource_type="channel",
                resource_id=channel.id,
                details={
                    "message_id": message_id,
                    "conversation_id": session.conversation_id,
                    "wa_id": wa_id,
                    "source": "whatsapp_business_app",
                    "message_type": str(echo.get("type") or "unknown"),
                    "mirrored_to_inbox": True,
                    "human_control_activated": not stale_after_resume,
                    "stale_after_ai_resume": stale_after_resume,
                },
            )
            complete_message_claim(db, message_id)
            db.commit()
            processed.append(
                {
                    "message_id": message_id,
                    "conversation_id": session.conversation_id,
                    "status": (
                        "stale_echo_mirrored"
                        if stale_after_resume
                        else "human_active"
                    ),
                    "mirrored": True,
                }
            )
        except Exception:
            db.rollback()
            release_message_claim(db, message_id)
            raise
    return processed


def _get_or_create_whatsapp_session(
    db,
    channel: AgentChannel,
    wa_id: str,
    phone_number_id: str,
    incoming_text: str,
):
    session = (
        db.query(WhatsAppSession)
        .filter(
            WhatsAppSession.company_id == channel.company_id,
            WhatsAppSession.agent_id == channel.agent_id,
            WhatsAppSession.phone_number_id == phone_number_id,
            WhatsAppSession.wa_id == wa_id,
        )
        .first()
    )
    if session is None:
        conversation = agent_runtime.get_or_create_conversation(
            db=db,
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            conversation_id=None,
            message=incoming_text,
        )
        session = WhatsAppSession(
            company_id=channel.company_id,
            agent_id=channel.agent_id,
            conversation_id=conversation.id,
            wa_id=wa_id,
            phone_number_id=phone_number_id,
        )
        db.add(session)
        db.flush()
    bind_conversation_source(
        db,
        conversation_id=session.conversation_id,
        company_id=channel.company_id,
        agent_id=channel.agent_id,
        channel_type="whatsapp",
        channel_id=channel.id,
        external_contact_id=wa_id,
    )
    return session


def _delivery_outcome_status(result: dict) -> str:
    if result.get("success"):
        return "sent"
    if result.get("unknown"):
        return "delivery_unknown"
    if result.get("permanent"):
        return "delivery_failed"
    return "delivery_retry"


def _process_delivery_statuses(
    db,
    channel: AgentChannel,
    value: dict,
) -> list[dict]:
    processed = []
    for status_event in value.get("statuses", []) or []:
        row = apply_provider_status(db, status_event)
        if row is None:
            continue
        audit_service.log(
            db=db,
            company_id=channel.company_id,
            action="whatsapp.delivery_status",
            resource_type="channel",
            resource_id=channel.id,
            details={
                "delivery_id": row.id,
                "conversation_id": row.conversation_id,
                "status": row.status,
                "provider_message_id": row.provider_message_id,
                "error_code": row.last_error_code,
            },
        )
        processed.append(
            {
                "delivery_id": row.id,
                "provider_message_id": row.provider_message_id,
                "status": row.status,
            }
        )
    if processed:
        db.commit()
    return processed


def _service_access_fallback(
    db,
    *,
    channel: AgentChannel,
    config: dict,
    wa_id: str,
    phone_number_id: str,
    incoming_text: str,
    message_id: str,
    error: HTTPException,
):
    db.rollback()
    lock_contact(db, channel.agent_id, wa_id)
    if not claim_message(
        db,
        message_id,
        channel.company_id,
        channel.agent_id,
        wa_id,
    ):
        retry = retry_delivery_for_inbound(
            db,
            inbound_external_message_id=message_id,
            config=config,
        )
        return None, retry

    session = _get_or_create_whatsapp_session(
        db,
        channel,
        wa_id,
        phone_number_id,
        incoming_text,
    )
    agent = agent_runtime.get_agent(db, channel.company_id, channel.agent_id)
    activate_human_handoff(session, reason="service_limit_or_entitlement")
    _ensure_handoff_record(
        db,
        channel=channel,
        conversation_id=session.conversation_id,
        reason="service_limit_or_entitlement",
    )
    reply_text = safe_service_unavailable_message(
        agent.system_prompt or "",
        incoming_text,
    )
    source_key = f"whatsapp:{message_id}"
    user_message = (
        db.query(AIMessage).filter(AIMessage.source_key == source_key).first()
    )
    if user_message is None:
        user_message = AIMessage(
            conversation_id=session.conversation_id,
            role="user",
            content=incoming_text,
            source_key=source_key,
        )
        db.add(user_message)
    reply_message = AIMessage(
        conversation_id=session.conversation_id,
        role="assistant",
        content=reply_text,
    )
    db.add(reply_message)
    db.flush()
    delivery = ensure_delivery(
        db,
        idempotency_key=f"wa-in:{message_id}:service-fallback-v1",
        company_id=channel.company_id,
        agent_id=channel.agent_id,
        conversation_id=session.conversation_id,
        channel_id=channel.id,
        message_id=reply_message.id,
        inbound_external_message_id=message_id,
        wa_id=wa_id,
    )
    audit_service.log(
        db=db,
        company_id=channel.company_id,
        action="whatsapp.customer_service_fallback_prepared",
        resource_type="channel",
        resource_id=channel.id,
        details={
            "message_id": message_id,
            "conversation_id": session.conversation_id,
            "delivery_id": delivery.id,
            "internal_status": error.status_code,
        },
    )
    complete_message_claim(db, message_id)
    db.commit()
    delivery_result = attempt_delivery(
        db,
        delivery_id=delivery.id,
        config=config,
    )
    return session.conversation_id, delivery_result


@router.get("")
def verify_webhook(
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if mode != "subscribe":
        raise HTTPException(status_code=403, detail="Invalid webhook mode")
    if not verify_token:
        raise HTTPException(status_code=403, detail="Verify token required")
    platform_token = str(_meta_settings().get("verify_token") or "")
    if platform_token and hmac.compare_digest(
        platform_token,
        str(verify_token),
    ):
        return int(challenge or "0")

    db = SessionLocal()
    try:
        for channel in get_whatsapp_channels(db):
            config = reveal_config(channel.config)
            stored_token = str(config.get("verify_token", ""))
            if stored_token and hmac.compare_digest(
                stored_token,
                str(verify_token),
            ):
                return int(challenge or "0")
        raise HTTPException(status_code=403, detail="Invalid verify token")
    finally:
        db.close()


def validate_webhook_request(
    raw_body: bytes,
    signature: str | None,
) -> tuple[dict, int]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "whatsapp_business_account"
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid WhatsApp webhook object",
        )
    _validate_payload_shape(payload)
    db = SessionLocal()
    matched_channels = 0
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                metadata = value.get("metadata", {}) or {}
                phone_number_id = str(metadata.get("phone_number_id", ""))
                if not phone_number_id:
                    continue
                channel = find_channel_by_phone_number_id(db, phone_number_id)
                if channel is None:
                    continue
                matched_channels += 1
                config = reveal_config(channel.config)
                if not verify_signature(
                    raw_body,
                    signature,
                    config.get("app_secret"),
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Invalid webhook signature",
                    )
    finally:
        db.close()
    return payload, matched_channels


def _validate_payload_shape(payload):
    try:
        entries = payload.get("entry", [])
        if not isinstance(entries, list):
            raise ValueError
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("changes", []),
                list,
            ):
                raise ValueError
            for change in entry.get("changes", []):
                if not isinstance(change, dict):
                    raise ValueError
                value = change.get("value") or {}
                if not isinstance(value, dict) or not isinstance(
                    value.get("metadata", {}),
                    dict,
                ):
                    raise ValueError
                for key in ("messages", "message_echoes", "statuses"):
                    messages = value.get(key, [])
                    if not isinstance(messages, list) or any(
                        not isinstance(item, dict) for item in messages
                    ):
                        raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(
            400,
            "Invalid WhatsApp webhook structure",
        ) from None


@router.post("")
async def receive_webhook(request: Request):
    raw_body = b""
    async for chunk in request.stream():
        raw_body += chunk
        if len(raw_body) > 1024 * 1024:
            raise HTTPException(
                413,
                "WhatsApp webhook payload is too large",
            )
    signature = request.headers.get("x-hub-signature-256")
    payload, matched_channels = await run_in_threadpool(
        validate_webhook_request,
        raw_body=raw_body,
        signature=signature,
    )
    if matched_channels == 0:
        return {
            "status": "ignored",
            "reason": "unknown_phone_number_id",
        }
    if whatsapp_job_queue.enabled:
        try:
            await run_in_threadpool(_mark_incoming_echoes, payload)
            job_id = whatsapp_job_queue.enqueue(
                body=raw_body.decode("utf-8"),
                signature=signature or "",
            )
        except (RedisError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail="WhatsApp processing queue unavailable",
            ) from exc
        return {"status": "accepted", "job_id": job_id}
    return await run_in_threadpool(
        process_webhook_payload,
        raw_body=raw_body,
        signature=signature,
    )


def _mark_incoming_echoes(payload):
    db = SessionLocal()
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "smb_message_echoes":
                    continue
                value = change.get("value") or {}
                phone = str(
                    (value.get("metadata") or {}).get("phone_number_id") or ""
                )
                if find_channel_by_phone_number_id(db, phone) is None:
                    continue
                for echo in value.get("message_echoes", []):
                    recipient = echo_recipient(echo)
                    event_id = str(echo.get("id") or "")
                    existing = (
                        db.query(WhatsAppInboundMessage)
                        .filter(
                            WhatsAppInboundMessage.external_message_id == event_id
                        )
                        .first()
                    )
                    if recipient and event_id and (
                        existing is None
                        or existing.status not in TERMINAL_INBOUND_STATUSES
                    ):
                        whatsapp_job_queue.mark_human(
                            phone,
                            recipient,
                            event_id,
                        )
    finally:
        db.close()


def _handle_existing_inbound_delivery(
    db,
    *,
    message_id: str,
    config: dict,
    processed: list[dict],
) -> bool:
    delivery_result = retry_delivery_for_inbound(
        db,
        inbound_external_message_id=message_id,
        config=config,
    )
    if delivery_result is None:
        processed.append(
            {"message_id": message_id, "status": "duplicate"}
        )
        return True
    if delivery_result.get("success"):
        processed.append(
            {
                "message_id": message_id,
                "status": "delivery_recovered",
                "delivery_id": delivery_result.get("delivery_id"),
            }
        )
        return True
    if delivery_result.get("unknown"):
        processed.append(
            {
                "message_id": message_id,
                "status": "delivery_unknown",
                "delivery_id": delivery_result.get("delivery_id"),
            }
        )
        return True
    if delivery_result.get("permanent"):
        processed.append(
            {
                "message_id": message_id,
                "status": "delivery_failed",
                "delivery_id": delivery_result.get("delivery_id"),
            }
        )
        return True
    if delivery_result.get("retryable"):
        raise RuntimeError("WhatsApp delivery retry failed")
    return True


def _persist_inbound_user_message(
    db,
    *,
    conversation_id: int,
    message_id: str,
    content: str,
) -> AIMessage:
    source_key = f"whatsapp:{message_id}"
    existing = (
        db.query(AIMessage)
        .filter(AIMessage.source_key == source_key)
        .first()
    )
    if existing is not None:
        if (
            existing.conversation_id != conversation_id
            or existing.role != "user"
            or str(existing.content or "").strip() != str(content or "").strip()
        ):
            raise RuntimeError("WhatsApp message source identity conflict")
        return existing
    row = AIMessage(
        conversation_id=conversation_id,
        role="user",
        content=content,
        source_key=source_key,
    )
    db.add(row)
    db.flush()
    return row


def process_webhook_payload(raw_body: bytes, signature: str | None):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "whatsapp_business_account"
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid WhatsApp webhook object",
        )
    _validate_payload_shape(payload)

    db = SessionLocal()
    processed = []
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {}) or {}
                metadata = value.get("metadata", {}) or {}
                phone_number_id = str(metadata.get("phone_number_id", ""))
                if not phone_number_id:
                    continue
                channel = find_channel_by_phone_number_id(
                    db,
                    phone_number_id,
                )
                if channel is None:
                    continue
                config = reveal_config(channel.config)
                if not verify_signature(
                    raw_body,
                    signature,
                    config.get("app_secret"),
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Invalid webhook signature",
                    )

                field = change.get("field")
                logger.info(
                    "WhatsApp event received; channel_id=%s field=%s",
                    channel.id,
                    (
                        field
                        if field
                        in {
                            "messages",
                            "smb_message_echoes",
                            "smb_app_state_sync",
                            "history",
                            "account_update",
                        }
                        else "other"
                    ),
                )
                if value.get("statuses"):
                    processed.extend(
                        _process_delivery_statuses(db, channel, value)
                    )
                if field == "smb_message_echoes":
                    processed.extend(
                        process_business_app_echo(
                            db=db,
                            channel=channel,
                            value=value,
                        )
                    )
                    continue
                if field not in (None, "messages"):
                    continue
                if not channel.enabled:
                    logger.info(
                        "WhatsApp automation inactive; channel_id=%s",
                        channel.id,
                    )
                    continue

                for message in value.get("messages", []):
                    message_id = str(message.get("id") or "").strip()
                    wa_id = str(message.get("from") or "").strip()
                    if not message_id or not wa_id:
                        continue
                    if not claim_message(
                        db=db,
                        message_id=message_id,
                        company_id=channel.company_id,
                        agent_id=channel.agent_id,
                        wa_id=wa_id,
                    ):
                        _handle_existing_inbound_delivery(
                            db,
                            message_id=message_id,
                            config=config,
                            processed=processed,
                        )
                        continue

                    if message.get("type") != "text":
                        complete_message_claim(db, message_id, status="ignored")
                        db.commit()
                        processed.append(
                            {
                                "message_id": message_id,
                                "status": "ignored_non_text",
                            }
                        )
                        continue
                    incoming_text = str(
                        (message.get("text", {}) or {}).get("body", "")
                    ).strip()
                    if not incoming_text:
                        complete_message_claim(db, message_id, status="ignored")
                        db.commit()
                        processed.append(
                            {
                                "message_id": message_id,
                                "status": "ignored_empty",
                            }
                        )
                        continue

                    try:
                        lock_contact(db, channel.agent_id, wa_id)
                        session = _get_or_create_whatsapp_session(
                            db,
                            channel,
                            wa_id,
                            phone_number_id,
                            incoming_text,
                        )
                        agent = agent_runtime.get_agent(
                            db,
                            channel.company_id,
                            channel.agent_id,
                        )

                        if config.get("coexistence") is True:
                            from backend.app.modules.channels.whatsapp_connection import (
                                whatsapp_connection_state,
                            )

                            if not whatsapp_connection_state(config).get(
                                "connected"
                            ):
                                activate_human_handoff(
                                    session,
                                    reason="coexistence_unverified",
                                )
                                logger.warning(
                                    "WhatsApp automation held for unverified Coexistence; channel_id=%s",
                                    channel.id,
                                )

                        if whatsapp_job_queue.human_marker(
                            phone_number_id,
                            wa_id,
                        ):
                            activate_human_handoff(
                                session,
                                reason="business_app_reply",
                            )

                        if requests_human(
                            incoming_text
                        ) and not human_handoff_active(session):
                            activate_human_handoff(
                                session,
                                reason="customer_request",
                            )
                            _ensure_handoff_record(
                                db,
                                channel=channel,
                                conversation_id=session.conversation_id,
                                reason="customer_request",
                            )
                            _persist_inbound_user_message(
                                db,
                                conversation_id=session.conversation_id,
                                message_id=message_id,
                                content=incoming_text,
                            )
                            acknowledgement = AIMessage(
                                conversation_id=session.conversation_id,
                                role="assistant",
                                content=human_handoff_acknowledgement(
                                    agent.system_prompt or "",
                                    incoming_text,
                                ),
                            )
                            db.add(acknowledgement)
                            db.flush()
                            delivery = ensure_delivery(
                                db,
                                idempotency_key=(
                                    f"wa-in:{message_id}:handoff-ack-v1"
                                ),
                                company_id=channel.company_id,
                                agent_id=channel.agent_id,
                                conversation_id=session.conversation_id,
                                channel_id=channel.id,
                                message_id=acknowledgement.id,
                                inbound_external_message_id=message_id,
                                wa_id=wa_id,
                            )
                            audit_service.log(
                                db=db,
                                company_id=channel.company_id,
                                action="whatsapp.handoff_requested",
                                resource_type="channel",
                                resource_id=channel.id,
                                details={
                                    "message_id": message_id,
                                    "conversation_id": session.conversation_id,
                                    "delivery_id": delivery.id,
                                },
                            )
                            complete_message_claim(db, message_id)
                            db.commit()
                            delivery_result = attempt_delivery(
                                db,
                                delivery_id=delivery.id,
                                config=config,
                            )
                            processed.append(
                                {
                                    "message_id": message_id,
                                    "conversation_id": session.conversation_id,
                                    "status": (
                                        "waiting_for_human"
                                        if delivery_result.get("success")
                                        else _delivery_outcome_status(
                                            delivery_result
                                        )
                                    ),
                                    "delivery_id": delivery.id,
                                }
                            )
                            if delivery_result.get("retryable"):
                                raise RuntimeError(
                                    "WhatsApp handoff acknowledgement delivery retry failed"
                                )
                            continue

                        if human_handoff_active(session):
                            extend_human_handoff(session)
                            _ensure_handoff_record(
                                db,
                                channel=channel,
                                conversation_id=session.conversation_id,
                                reason=(
                                    session.handoff_reason or "human_active"
                                ),
                            )
                            _persist_inbound_user_message(
                                db,
                                conversation_id=session.conversation_id,
                                message_id=message_id,
                                content=incoming_text,
                            )
                            audit_service.log(
                                db=db,
                                company_id=channel.company_id,
                                action="whatsapp.message_routed_to_human",
                                resource_type="channel",
                                resource_id=channel.id,
                                details={
                                    "message_id": message_id,
                                    "conversation_id": session.conversation_id,
                                    "wa_id": wa_id,
                                },
                            )
                            complete_message_claim(db, message_id)
                            db.commit()
                            processed.append(
                                {
                                    "message_id": message_id,
                                    "conversation_id": session.conversation_id,
                                    "status": "waiting_for_human",
                                }
                            )
                            continue

                        result = agent_runtime.chat(
                            db=db,
                            company_id=channel.company_id,
                            agent_id=channel.agent_id,
                            message=incoming_text,
                            conversation_id=session.conversation_id,
                            commit=False,
                            user_message_source_key=f"whatsapp:{message_id}",
                        )
                    except HTTPException as exc:
                        if is_service_access_error(exc):
                            conversation_id, delivery_result = (
                                _service_access_fallback(
                                    db,
                                    channel=channel,
                                    config=config,
                                    wa_id=wa_id,
                                    phone_number_id=phone_number_id,
                                    incoming_text=incoming_text,
                                    message_id=message_id,
                                    error=exc,
                                )
                            )
                            if delivery_result and delivery_result.get(
                                "retryable"
                            ):
                                raise RuntimeError(
                                    "WhatsApp service fallback delivery retry failed"
                                )
                            processed.append(
                                {
                                    "message_id": message_id,
                                    "conversation_id": conversation_id,
                                    "status": (
                                        "waiting_for_human"
                                        if delivery_result
                                        and delivery_result.get("success")
                                        else _delivery_outcome_status(
                                            delivery_result or {}
                                        )
                                    ),
                                }
                            )
                            continue
                        db.rollback()
                        release_message_claim(db, message_id)
                        audit_service.log(
                            db=db,
                            company_id=channel.company_id,
                            action="whatsapp.runtime_failed",
                            resource_type="channel",
                            resource_id=channel.id,
                            details={
                                "message_id": message_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                        db.commit()
                        raise
                    except Exception as exc:
                        db.rollback()
                        release_message_claim(db, message_id)
                        audit_service.log(
                            db=db,
                            company_id=channel.company_id,
                            action="whatsapp.runtime_failed",
                            resource_type="channel",
                            resource_id=channel.id,
                            details={
                                "message_id": message_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                        db.commit()
                        raise

                    db.refresh(session)
                    if human_handoff_active(
                        session
                    ) or whatsapp_job_queue.human_marker(
                        phone_number_id,
                        wa_id,
                    ):
                        db.rollback()
                        lock_contact(db, channel.agent_id, wa_id)
                        if claim_message(
                            db,
                            message_id,
                            channel.company_id,
                            channel.agent_id,
                            wa_id,
                        ):
                            session = _get_or_create_whatsapp_session(
                                db,
                                channel,
                                wa_id,
                                phone_number_id,
                                incoming_text,
                            )
                            activate_human_handoff(
                                session,
                                reason="human_takeover_during_generation",
                            )
                            _ensure_handoff_record(
                                db,
                                channel=channel,
                                conversation_id=session.conversation_id,
                                reason=session.handoff_reason,
                            )
                            _persist_inbound_user_message(
                                db,
                                conversation_id=session.conversation_id,
                                message_id=message_id,
                                content=incoming_text,
                            )
                            complete_message_claim(db, message_id)
                            db.commit()
                        processed.append(
                            {
                                "message_id": message_id,
                                "status": "waiting_for_human",
                            }
                        )
                        continue

                    delivery = ensure_delivery(
                        db,
                        idempotency_key=f"wa-in:{message_id}:ai-reply-v1",
                        company_id=channel.company_id,
                        agent_id=channel.agent_id,
                        conversation_id=result["conversation_id"],
                        channel_id=channel.id,
                        message_id=result["response"]["id"],
                        inbound_external_message_id=message_id,
                        wa_id=wa_id,
                    )
                    audit_service.log(
                        db=db,
                        company_id=channel.company_id,
                        action="whatsapp.reply_prepared",
                        resource_type="channel",
                        resource_id=channel.id,
                        details={
                            "message_id": message_id,
                            "conversation_id": result["conversation_id"],
                            "delivery_id": delivery.id,
                        },
                    )
                    complete_message_claim(db, message_id)
                    db.commit()

                    delivery_result = attempt_delivery(
                        db,
                        delivery_id=delivery.id,
                        config=config,
                    )
                    if delivery_result.get("success"):
                        audit_service.log(
                            db=db,
                            company_id=channel.company_id,
                            action="whatsapp.reply_sent",
                            resource_type="channel",
                            resource_id=channel.id,
                            details={
                                "message_id": message_id,
                                "conversation_id": result[
                                    "conversation_id"
                                ],
                                "delivery_id": delivery.id,
                                "provider_message_id": delivery_result.get(
                                    "provider_message_id"
                                ),
                                "status_code": delivery_result.get(
                                    "status_code"
                                ),
                            },
                        )
                        db.commit()
                        processed.append(
                            {
                                "message_id": message_id,
                                "agent_id": channel.agent_id,
                                "conversation_id": result[
                                    "conversation_id"
                                ],
                                "delivery_id": delivery.id,
                                "reply_sent": True,
                            }
                        )
                        continue

                    audit_service.log(
                        db=db,
                        company_id=channel.company_id,
                        action="whatsapp.reply_delivery_unresolved",
                        resource_type="channel",
                        resource_id=channel.id,
                        details={
                            "message_id": message_id,
                            "conversation_id": result["conversation_id"],
                            "delivery_id": delivery.id,
                            "delivery_status": delivery_result.get("status"),
                            "retryable": delivery_result.get("retryable"),
                            "error_code": delivery_result.get("error_code"),
                        },
                    )
                    db.commit()
                    if delivery_result.get("retryable"):
                        raise RuntimeError(
                            "WhatsApp reply delivery retry scheduled"
                        )
                    processed.append(
                        {
                            "message_id": message_id,
                            "agent_id": channel.agent_id,
                            "conversation_id": result["conversation_id"],
                            "delivery_id": delivery.id,
                            "status": _delivery_outcome_status(
                                delivery_result
                            ),
                        }
                    )
        return {"status": "ok", "processed": processed}
    finally:
        db.close()
