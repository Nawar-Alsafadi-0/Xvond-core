from pathlib import Path


SOURCE = Path("scripts/production_acceptance.py").read_text(encoding="utf-8")


def test_acceptance_is_pre_live_by_default_and_has_explicit_post_live_mode():
    assert "require_live: bool = False" in SOURCE
    assert '"mode": "post-live" if require_live else "pre-live"' in SOURCE
    assert "company_ok = setup_ready" in SOURCE
    assert 'lifecycle == "live"' in SOURCE
    assert '"--require-live"' in SOURCE


def test_acceptance_checks_platform_operations_health():
    assert 'checks["whatsapp_worker"]' in SOURCE
    assert 'checks["automation_scheduler"]' in SOURCE
    assert "automation_scheduler_health.status()" in SOURCE
    assert 'checks["backups"]' in SOURCE
    assert 'checks["open_incidents"]' in SOURCE
    assert "UNRESOLVED_EXTERNAL" in SOURCE
    assert "UNRESOLVED_DELIVERY" in SOURCE
    assert "ManagedChannelOutboundDelivery" in SOURCE
    assert '"unresolved_managed_channel_deliveries"' in SOURCE


def test_acceptance_requires_workflow_health_for_actions_or_managed_channels():
    assert "def _workflow_engine_check" in SOURCE
    assert 'AgentToolAssignment.tool_name == "action_request"' in SOURCE
    assert "N8N_CHANNEL_ADAPTER" in SOURCE
    assert "managed_channels" in SOURCE
    assert '"business_actions"' in SOURCE
    assert '"managed_channels"' in SOURCE
    assert "n8n_gateway.configured()" in SOURCE
    assert 'action="health_check"' in SOURCE
    assert 'checks["workflow_engine"]' in SOURCE
    assert '"status": "not_required"' in SOURCE
    assert '"status": "not_configured"' in SOURCE


def test_acceptance_never_serializes_raw_exception_messages():
    assert "safe_error_label" in SOURCE
    assert '"error": str(exc)' not in SOURCE
    assert "str(exc)[:500]" not in SOURCE
