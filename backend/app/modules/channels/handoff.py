import re
from datetime import datetime, timedelta

from redis.exceptions import RedisError


DEFAULT_BUSINESS_APP_TAKEOVER_MINUTES = 10
_ARABIC_DIACRITICS = re.compile(r"[\u064b-\u065f\u0670]")
_HUMAN_REQUEST_PATTERNS = (
    "موظف",
    "موظفة",
    "انسان",
    "بشري",
    "خدمة العملاء",
    "مسؤول",
    "مدير",
    "human",
    "agent",
    "representative",
    "customer service",
    "real person",
    "live person",
)


def normalize_message(value: str) -> str:
    text = _ARABIC_DIACRITICS.sub("", str(value or "").lower())
    return " ".join(text.split())


def requests_human(message: str) -> bool:
    text = normalize_message(message)
    return any(pattern in text for pattern in _HUMAN_REQUEST_PATTERNS)


def echo_recipient(message_echo: dict) -> str | None:
    for key in ("to", "recipient_id"):
        value = str(message_echo.get(key) or "").strip()
        if value:
            return value
    return None


def _complete_expired_handoff_records(session, current: datetime) -> None:
    """Close durable handoff rows when a timed Business App takeover expires."""

    company_id = getattr(session, "company_id", None)
    conversation_id = getattr(session, "conversation_id", None)
    if company_id is None or conversation_id is None:
        return

    try:
        from sqlalchemy.orm import object_session

        db = object_session(session)
    except Exception:
        db = None
    if db is None:
        return

    from backend.app.modules.tools.business_models import HumanHandoff

    rows = (
        db.query(HumanHandoff)
        .filter(
            HumanHandoff.company_id == company_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(["pending", "in_progress"]),
        )
        .all()
    )
    for row in rows:
        row.status = "completed"
        row.completed_at = current
        row.updated_at = current


def activate_human_handoff(
    session,
    reason: str,
    now: datetime | None = None,
    minutes: int | None = None,
    human_message: bool = False,
):
    """Put a conversation under human control.

    A real reply from the WhatsApp Business App pauses AI for ten minutes by
    default. Every newer Business App reply resets that window. Customer
    requests, portal takeovers, and service fallbacks remain open-ended unless
    a caller explicitly supplies a positive ``minutes`` value.
    """
    current = now or datetime.utcnow()
    previous_state = getattr(session, "automation_state", None)
    previous_reason = getattr(session, "handoff_reason", None)
    previous_deadline = getattr(session, "human_takeover_until", None)

    takeover_minutes = minutes
    if reason == "business_app_reply" and takeover_minutes is None:
        takeover_minutes = DEFAULT_BUSINESS_APP_TAKEOVER_MINUTES

    preserve_business_app_deadline = (
        reason == "business_app_reply"
        and not human_message
        and previous_state == "human"
        and previous_reason == "business_app_reply"
        and previous_deadline is not None
    )

    session.automation_state = "human"
    session.handoff_reason = reason
    if preserve_business_app_deadline:
        session.human_takeover_until = previous_deadline
    elif takeover_minutes is not None and int(takeover_minutes) > 0:
        session.human_takeover_until = current + timedelta(
            minutes=int(takeover_minutes)
        )
    else:
        session.human_takeover_until = None
    session.updated_at = current

    if human_message:
        session.last_human_message_at = current

    return session


def human_handoff_active(
    session,
    now: datetime | None = None,
) -> bool:
    if getattr(session, "automation_state", None) != "human":
        return False

    deadline = getattr(session, "human_takeover_until", None)
    if deadline is None:
        return True

    current = now or datetime.utcnow()
    if current < deadline:
        return True

    _complete_expired_handoff_records(session, current)
    resume_ai(session, now=current)
    return False


def extend_human_handoff(
    session,
    now: datetime | None = None,
):
    return activate_human_handoff(
        session,
        reason=session.handoff_reason or "human_active",
        now=now,
    )


def resume_ai(
    session,
    now: datetime | None = None,
):
    current = now or datetime.utcnow()
    session.automation_state = "ai"
    session.handoff_reason = None
    session.human_takeover_until = None
    session.ai_resumed_at = current

    # Preserve the exact ingress echo that was pending when an operator chose
    # Return-to-AI. If that old echo is processed later it may still be mirrored
    # to the Inbox, but it must not undo the operator's newer control decision.
    marker = None
    phone_number_id = str(getattr(session, "phone_number_id", "") or "")
    wa_id = str(getattr(session, "wa_id", "") or "")
    if phone_number_id and wa_id:
        try:
            from backend.app.modules.channels.whatsapp_queue import whatsapp_job_queue

            marker = whatsapp_job_queue.human_marker(phone_number_id, wa_id)
        except RedisError:
            marker = None
    session.ai_resume_echo_id = marker
    session.updated_at = current
    return session
