from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from backend.app.modules.channels.handoff import (
    DEFAULT_BUSINESS_APP_TAKEOVER_MINUTES,
    activate_human_handoff,
    echo_recipient,
    extend_human_handoff,
    human_handoff_active,
    requests_human,
    resume_ai,
)


def make_session():
    return SimpleNamespace(
        automation_state="ai",
        handoff_reason=None,
        human_takeover_until=None,
        last_human_message_at=None,
        ai_resumed_at=None,
        ai_resume_echo_id=None,
        updated_at=None,
    )


def test_customer_can_request_human_in_arabic_or_english():
    assert requests_human("بدي احكي مع موظف لو سمحت") is True
    assert requests_human("Can I speak with a human?") is True
    assert requests_human("شو أوقات الدوام؟") is False


def test_business_app_reply_auto_resumes_after_ten_minutes():
    now = datetime(2026, 8, 24, 12, 0, 0)
    session = make_session()

    activate_human_handoff(
        session,
        reason="business_app_reply",
        now=now,
        human_message=True,
    )

    assert session.automation_state == "human"
    assert session.handoff_reason == "business_app_reply"
    assert session.last_human_message_at == now
    assert session.human_takeover_until == now + timedelta(
        minutes=DEFAULT_BUSINESS_APP_TAKEOVER_MINUTES
    )
    assert human_handoff_active(
        session,
        now=now + timedelta(minutes=9, seconds=59),
    ) is True
    assert human_handoff_active(
        session,
        now=now + timedelta(minutes=10),
    ) is False
    assert session.automation_state == "ai"
    assert session.handoff_reason is None


def test_new_business_app_reply_resets_takeover_window():
    now = datetime(2026, 8, 24, 12, 0, 0)
    session = make_session()
    activate_human_handoff(
        session,
        reason="business_app_reply",
        now=now,
        human_message=True,
    )
    original_deadline = session.human_takeover_until

    later = now + timedelta(minutes=7)
    activate_human_handoff(
        session,
        reason="business_app_reply",
        now=later,
        human_message=True,
    )

    assert session.last_human_message_at == later
    assert session.human_takeover_until == later + timedelta(minutes=10)
    assert session.human_takeover_until > original_deadline


def test_customer_message_does_not_extend_business_app_takeover_window():
    now = datetime(2026, 8, 24, 12, 0, 0)
    session = make_session()
    activate_human_handoff(
        session,
        reason="business_app_reply",
        now=now,
        human_message=True,
    )
    original_deadline = session.human_takeover_until

    extend_human_handoff(session, now=now + timedelta(minutes=5))

    assert session.human_takeover_until == original_deadline


def test_customer_requested_handoff_remains_explicit_until_resumed():
    now = datetime(2026, 8, 24, 12, 0, 0)
    session = make_session()
    activate_human_handoff(
        session,
        reason="customer_request",
        now=now,
    )

    assert session.human_takeover_until is None
    assert human_handoff_active(
        session,
        now=now + timedelta(days=7),
    ) is True

    resume_ai(session, now=now + timedelta(days=7))

    assert session.automation_state == "ai"
    assert session.handoff_reason is None
    assert session.human_takeover_until is None


def test_echo_recipient_uses_customer_destination_only():
    assert echo_recipient({"to": "96890000000"}) == "96890000000"
    assert echo_recipient({"recipient_id": "96891111111"}) == "96891111111"
    assert echo_recipient({"from": "business-number"}) is None


def test_webhook_supports_meta_coexistence_echoes():
    source = Path(
        "backend/app/api/whatsapp_webhook.py"
    ).read_text(encoding="utf-8")

    assert '"smb_message_echoes"' in source
    assert "process_business_app_echo" in source
    assert "whatsapp_business_app" in source
