from pathlib import Path

from backend.app.api.admin_ai_employee_profile import (
    CLARIFICATION_STYLES,
    DEFAULT_CUSTOMER_CONTROLS,
    OFF_TOPIC_BEHAVIORS,
    RESPONSE_LENGTHS,
)


PROFILE_API = Path("backend/app/api/admin_ai_employee_profile.py")
EMPLOYEE_UI = Path("frontend/admin/employee-capabilities.js")
READINESS = Path("backend/app/core/readiness.py")


def test_admin_created_ai_employees_default_to_customer_portal_visibility():
    assert DEFAULT_CUSTOMER_CONTROLS["can_view_conversations"] is True
    assert DEFAULT_CUSTOMER_CONTROLS["can_view_usage"] is True
    assert DEFAULT_CUSTOMER_CONTROLS["can_enable_disable"] is True
    assert DEFAULT_CUSTOMER_CONTROLS["can_edit_prompt"] is False
    assert DEFAULT_CUSTOMER_CONTROLS["can_change_provider"] is False
    assert DEFAULT_CUSTOMER_CONTROLS["can_change_model"] is False


def test_profile_api_ensures_agent_config_and_behavior_settings():
    source = PROFILE_API.read_text(encoding="utf-8-sig")
    assert "def _ensure_agent_config" in source
    assert "def _set_agent_behavior" in source
    assert "def _agent_behavior" in source
    assert '"response_length": "concise"' in source
    assert '"clarification_style": "smart"' in source
    assert '"off_topic_behavior": "business_redirect"' in source
    assert "settings.update(_behavior_values(data))" in source
    assert "controls.update(row.customer_controls or {})" in source


def test_employee_behavior_defaults_are_business_focused_and_simple():
    assert "concise" in RESPONSE_LENGTHS
    assert "smart" in CLARIFICATION_STYLES
    rule = OFF_TOPIC_BEHAVIORS["business_redirect"].lower()
    assert "personal, emotional or unrelated messages" in rule
    assert "therapist" in rule
    assert "personal support conversation" in rule
    assert "redirect naturally" in rule
    assert "business role" in rule


def test_employee_information_ui_exposes_only_simple_global_behavior_controls():
    source = EMPLOYEE_UI.read_text(encoding="utf-8-sig")
    assert 'id="simple-response-length"' in source
    assert 'id="simple-clarification-style"' in source
    assert 'id="simple-off-topic"' in source
    assert "response_length:" in source
    assert "clarification_style:" in source
    assert "off_topic_behavior:" in source
    assert "Business focused — recommended" in source


def test_readiness_exposes_canonical_and_legacy_profile_flags():
    source = READINESS.read_text(encoding="utf-8-sig")
    assert '"company_profile_ready": company_profile_ready' in source
    assert '"profile_ready": company_profile_ready' in source


def test_managed_employee_profile_ensures_safe_human_handoff_default():
    source = PROFILE_API.read_text(encoding="utf-8-sig")
    assert "def _ensure_human_handoff" in source
    assert 'tool_name="human_handoff"' in source
    assert "enabled=True" in source
    assert source.count("_ensure_human_handoff(db, agent.id)") >= 2


def test_admin_profile_employee_is_explicitly_managed_delivery():
    source = PROFILE_API.read_text(encoding="utf-8-sig")
    assert '"onboarding_source": "managed"' in source
    assert '"delivery_mode": "managed"' in source
