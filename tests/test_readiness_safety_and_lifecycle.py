import inspect

from backend.app.core import readiness


def test_provider_readiness_uses_safe_error_label():
    source = inspect.getsource(readiness._provider_runtime)
    assert "safe_error_label(exc)" in source
    assert "str(exc)" not in source


def test_company_readiness_exposes_lifecycle_and_runtime_separately():
    source = inspect.getsource(readiness.company_readiness)
    assert '"active": company.active' in source
    assert '"lifecycle_status": company.lifecycle_status' in source
    assert '"lifecycle_updated_at": company.lifecycle_updated_at' in source


def test_readiness_language_distinguishes_runtime_from_commercial_state():
    source = inspect.getsource(readiness.company_readiness)
    assert "Company runtime is currently stopped" in source


def test_coexistence_needs_roundtrip_and_real_echo_before_customer_ready(monkeypatch):
    monkeypatch.setattr(readiness, "customer_roundtrip_verified", lambda _config: True)
    assert readiness._channel_customer_accepted(
        channel_type="whatsapp",
        channel_config={"coexistence": True},
        connected=True,
        connection={"coexistence_ready": False},
    ) is False
    assert readiness._channel_customer_accepted(
        channel_type="whatsapp",
        channel_config={"coexistence": True},
        connected=True,
        connection={"coexistence_ready": True},
    ) is True


def test_connected_channel_needs_real_roundtrip_before_customer_accepted(monkeypatch):
    monkeypatch.setattr(readiness, "customer_roundtrip_verified", lambda _config: False)
    assert readiness._channel_customer_accepted(
        channel_type="website",
        channel_config={},
        connected=True,
        connection=None,
    ) is False

    monkeypatch.setattr(readiness, "customer_roundtrip_verified", lambda _config: True)
    assert readiness._channel_customer_accepted(
        channel_type="website",
        channel_config={},
        connected=True,
        connection=None,
    ) is True
    assert readiness._channel_customer_accepted(
        channel_type="whatsapp",
        channel_config={"coexistence": False},
        connected=True,
        connection={"coexistence_ready": False},
    ) is True
    assert readiness._channel_customer_accepted(
        channel_type="whatsapp",
        channel_config={"coexistence": True},
        connected=False,
        connection={"coexistence_ready": True},
    ) is False


def test_ready_for_customer_rejects_enabled_channel_pending_acceptance():
    source = inspect.getsource(readiness.company_readiness)
    assert "unaccepted_enabled_channels" in source
    assert "and not unaccepted_enabled_channels" in source
    assert "human takeover acceptance is pending" in source


def test_company_readiness_blocks_only_unusable_business_action_state():
    source = inspect.getsource(readiness.company_readiness)
    assert 'action_request_assigned = "action_request" in enabled_tool_names' in source
    assert "legacy_business_tools" in source
    assert "tools_ready = bool(" in source
    assert "not action_request_assigned or ready_action" in source
    assert "Legacy business tools are still enabled" in source
    assert "Business Actions are enabled, but no configured customer action is runtime-ready" in source
    assert "and tools_ready" in source
    assert '"tools_ready": tools_ready' in source


def test_human_handoff_is_not_treated_as_a_missing_business_action():
    source = inspect.getsource(readiness.company_readiness)
    assert 'if tools and not ready_action:' not in source
    assert 'action_request_assigned and not ready_action' in source


def test_company_readiness_uses_employee_delivery_mode_not_only_company_source():
    source = inspect.getsource(readiness.company_readiness)
    assert "is_self_service_employee(company, agent_config)" in source
    assert '"self_service" if self_service_agent else "managed"' in source
    assert "Company profile is incomplete for managed delivery" in source
