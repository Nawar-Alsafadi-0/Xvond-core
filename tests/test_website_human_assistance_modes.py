import pytest
from pydantic import ValidationError

from backend.app.api.public_channels import _safe_unavailable_message
from backend.app.api.website_widget import WebsiteSetup, website_behavior


def _setup(**overrides):
    values = {
        "allowed_domain": "example.com",
        "human_assistance_mode": "direct_handoff",
    }
    values.update(overrides)
    return WebsiteSetup(**values)


def test_website_behavior_keeps_small_talk_professional():
    prompt = website_behavior({})

    assert "warm, professional and business-appropriate" in prompt
    assert "يا حبيبي" in prompt
    assert "Do not mechanically mirror casual small talk" in prompt
    assert "Do not ask a question that the visitor has already answered" in prompt


def test_website_setup_accepts_supported_human_assistance_modes():
    for mode in ("direct_handoff", "contact_only", "ai_only"):
        assert _setup(human_assistance_mode=mode).human_assistance_mode == mode


def test_website_setup_rejects_unknown_human_assistance_mode():
    with pytest.raises(ValidationError):
        _setup(human_assistance_mode="always_transfer")


def test_contact_only_behavior_uses_configured_contacts_without_live_handoff():
    prompt = website_behavior(
        {
            "human_assistance_mode": "contact_only",
            "contact_whatsapp": "+96891118075",
            "contact_email": "support@example.com",
        }
    )
    assert "Contact Only" in prompt
    assert "Never create or claim a live human handoff" in prompt
    assert "WhatsApp: +96891118075" in prompt
    assert "Email: support@example.com" in prompt


def test_ai_only_behavior_does_not_expose_configured_contact_details():
    prompt = website_behavior(
        {
            "human_assistance_mode": "ai_only",
            "contact_phone": "+96890000000",
        }
    )
    assert "AI Only" in prompt
    assert "+96890000000" not in prompt


def test_contact_only_service_fallback_gives_contacts_without_transfer_claim():
    text = _safe_unavailable_message(
        "I need help",
        {
            "human_assistance_mode": "contact_only",
            "contact_email": "support@example.com",
        },
    )
    assert "support@example.com" in text
    assert "forwarded" not in text.lower()


def test_ai_only_service_fallback_does_not_claim_transfer_or_contact():
    text = _safe_unavailable_message(
        "مرحبا",
        {
            "human_assistance_mode": "ai_only",
            "contact_phone": "+96890000000",
        },
    )
    assert "تم تحويل" not in text
    assert "+96890000000" not in text
