from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8-sig")


def test_admin_channel_list_verifies_whatsapp_connection():
    api = source("backend/app/api/admin_channels.py")
    assert "whatsapp_connection_state" in api
    assert 'verify_connection=item.channel_type == "whatsapp"' in api
    assert '"connected": configured' in api
    assert '"connection_status"' in api


def test_whatsapp_activation_requires_verified_meta_connection_not_just_config():
    api = source("backend/app/api/admin_channels.py")
    blockers = api.split("def _activation_blockers", 1)[1].split("@router.post", 1)[0]
    assert 'channel_type == "whatsapp"' in blockers
    assert "whatsapp_connection_state(" in blockers
    assert "verify_remote=True" in blockers
    assert 'connection["connected"] is not True' in blockers
    assert 'connection.get("connection_issue")' in blockers


def test_company_readiness_uses_verified_whatsapp_connection_truth():
    readiness = source("backend/app/core/readiness.py")
    assert "from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state" in readiness
    assert "whatsapp_meta_onboarding_complete" not in readiness
    assert "whatsapp_connection_state(" in readiness
    assert "verify_remote=True" in readiness
    assert 'connection["connected"] is True' in readiness
    assert '"connection_status"' in readiness
    assert '"connection_issue"' in readiness
    assert 'connection.get("coexistence_ready") is True' in readiness


def test_delivery_readiness_separates_live_transport_from_customer_acceptance():
    delivery = source("backend/app/api/admin_delivery_readiness.py")
    assert "from backend.app.modules.channels.whatsapp_connection import whatsapp_connection_state" in delivery
    assert "whatsapp_meta_onboarding_complete" not in delivery
    channel_state = delivery.split("def _channel_state", 1)[1].split("def _assert_workflow_runtime_ready", 1)[0]
    assert 'row.channel_type == "whatsapp"' in channel_state
    assert "whatsapp_connection_state(config, verify_remote=True)" in channel_state
    assert 'connection["connected"]' in channel_state
    assert "_channel_customer_accepted" in channel_state
    assert 'if connected:' in channel_state
    assert 'if accepted:' in channel_state
    assert '"customer_ready": fully_customer_ready' in channel_state


def test_admin_ui_separates_configuration_activation_and_meta_connection():
    ui = source("frontend/admin/company-control-center.js")
    assert "Disconnected · Invalid token" in ui
    assert "Configured only" in ui
    assert "Local channel active" in ui
    assert "Credentials:" in ui
    assert "Connect with Meta" in ui
    assert "x.enabled===true&&x.connected===true" in ui


def test_admin_attention_panel_flags_locally_active_disconnected_whatsapp():
    ui = source("frontend/admin/control-center-polish.js")
    assert "channel.channel_type==='whatsapp'" in ui
    assert "channel.enabled" in ui
    assert "channel.connected!==true" in ui
    assert "WhatsApp connection needs attention" in ui
    assert "channel.connection_issue" in ui


def test_customer_status_uses_safe_reconnect_state_without_raw_meta_diagnostics():
    api = source("backend/app/api/customer_meta_whatsapp.py")
    ui = source("frontend/customer/meta-whatsapp.js")
    assert '"connection_status": connection["connection_status"]' in api
    assert '"connection_issue": connection["connection_issue"]' in api
    assert '"meta_error_code": connection["meta_error_code"]' in api
    assert 'config.connection_status === "invalid_token"' in ui
    assert "config.connection_issue" not in ui
    assert "إعادة ربط" in ui
