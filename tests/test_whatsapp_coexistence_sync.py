from datetime import datetime, timedelta
from types import SimpleNamespace

from backend.app.api.whatsapp_webhook import (
    _business_app_echo_content,
    _business_app_echo_created_at,
)
from backend.app.modules.channels.handoff import (
    activate_human_handoff,
    human_handoff_active,
    resume_ai,
)


def test_business_app_text_echo_preserves_message_body():
    echo = {
        "type": "text",
        "text": {"body": "Manual reply from WhatsApp Business"},
    }

    assert _business_app_echo_content(echo) == "Manual reply from WhatsApp Business"


def test_business_app_media_echo_preserves_caption_or_uses_safe_label():
    captioned = {
        "type": "image",
        "image": {"caption": "Product photo"},
    }
    audio = {"type": "audio", "audio": {"id": "media-id"}}

    assert _business_app_echo_content(captioned) == "Product photo"
    assert _business_app_echo_content(audio) == "[Audio sent from WhatsApp Business]"


def test_business_app_echo_timestamp_is_normalized_to_utc_naive_datetime():
    echo = {"timestamp": "1700000000"}

    assert _business_app_echo_created_at(echo) == datetime.utcfromtimestamp(1700000000)
    assert _business_app_echo_created_at({"timestamp": "bad"}) is None


def test_business_app_human_takeover_expires_after_inactivity():
    now = datetime.utcnow()
    session = SimpleNamespace(
        automation_state="ai",
        handoff_reason=None,
        human_takeover_until=None,
        updated_at=None,
        last_human_message_at=None,
        ai_resumed_at=None,
        ai_resume_echo_id=None,
    )

    activate_human_handoff(
        session,
        reason="business_app_reply",
        now=now,
        human_message=True,
    )

    assert session.automation_state == "human"
    assert session.human_takeover_until == now + timedelta(minutes=10)
    assert session.last_human_message_at == now
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

    resume_ai(session, now=now + timedelta(minutes=11))

    assert session.automation_state == "ai"
    assert human_handoff_active(session) is False
