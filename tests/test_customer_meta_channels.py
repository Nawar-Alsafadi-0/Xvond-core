from types import SimpleNamespace

from backend.app.api import customer_meta_channels as module
from backend.app.api.customer_meta_channels import (
    MetaDiscoverRequest,
    _normalize_channel_type,
    _public_asset,
    discover_assets,
)
from fastapi import HTTPException


def test_meta_customer_channel_types_are_limited():
    assert _normalize_channel_type("instagram") == "instagram"
    assert _normalize_channel_type("messenger") == "messenger"
    try:
        _normalize_channel_type("whatsapp")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("WhatsApp must keep its Embedded Signup flow")


def test_meta_asset_response_never_exposes_access_token():
    asset = {
        "page_id": "123",
        "page_name": "Demo",
        "page_access_token": "secret-token",
        "instagram_id": "456",
        "instagram_username": "demo",
    }
    public = _public_asset(asset, "instagram")
    assert public["page_id"] == "123"
    assert public["instagram_id"] == "456"
    assert "page_access_token" not in public



def test_meta_channel_action_model():
    from backend.app.api.customer_meta_channels import MetaChannelAction
    item = MetaChannelAction(agent_id=7, channel_type="instagram")
    assert item.agent_id == 7
    assert item.channel_type == "instagram"


def test_messenger_connect_config_uses_dedicated_business_login_config(monkeypatch):
    from backend.app.api import customer_meta_channels as module
    from backend.app.core.config.settings import settings

    monkeypatch.setattr(settings, "META_MESSENGER_CONFIG_ID", "messenger-config-123")
    monkeypatch.setattr(module, "_meta_settings", lambda: {
        "app_id": "app",
        "app_secret": "secret",
        "graph_api_version": "v26.0",
    })
    monkeypatch.setattr(module.n8n_gateway, "configured", lambda: True)
    monkeypatch.setattr(settings, "WORKFLOW_PUBLIC_URL", "https://workflow.xvond.com")

    config = module._meta_connect_settings("messenger")
    assert config["ready"] is True
    assert config["messenger_config_id"] == "messenger-config-123"


def test_messenger_connect_config_fails_closed_without_business_login_config(monkeypatch):
    from backend.app.api import customer_meta_channels as module
    from backend.app.core.config.settings import settings

    monkeypatch.setattr(settings, "META_MESSENGER_CONFIG_ID", "")
    monkeypatch.setattr(module, "_meta_settings", lambda: {
        "app_id": "app",
        "app_secret": "secret",
        "graph_api_version": "v26.0",
    })
    monkeypatch.setattr(module.n8n_gateway, "configured", lambda: True)
    monkeypatch.setattr(settings, "WORKFLOW_PUBLIC_URL", "https://workflow.xvond.com")

    config = module._meta_connect_settings("messenger")
    assert config["ready"] is False
    assert "messenger_config_id" in config["missing"]


def test_messenger_discovery_uses_channel_specific_connect_settings(monkeypatch):
    requested = []

    class DummyDb:
        def close(self):
            pass

    def fake_meta_connect_settings(channel_type):
        requested.append(channel_type)
        return {
            "ready": True,
            "graph_api_version": "v26.0",
            "app_id": "app-id",
            "app_secret": "app-secret",
            "messenger_config_id": "config-id",
        }

    monkeypatch.setattr(module, "_meta_connect_settings", fake_meta_connect_settings)
    monkeypatch.setattr(module, "SessionLocal", DummyDb)
    monkeypatch.setattr(module, "_customer_channel", lambda *args, **kwargs: (object(), object(), object()))
    monkeypatch.setattr(module, "_page_assets", lambda *args, **kwargs: [])

    result = discover_assets(
        MetaDiscoverRequest(
            agent_id=7,
            channel_type="messenger",
            user_access_token="test-user-token",
        ),
        current_user=SimpleNamespace(company_id=11),
    )

    assert requested == ["messenger"]
    assert result == {"channel_type": "messenger", "assets": []}


def test_meta_channel_frontend_fallback_does_not_force_cors_and_is_retryable():
    from pathlib import Path

    source = Path("frontend/customer/meta-channels.js").read_text(encoding="utf-8")
    assert 'crossOrigin = "anonymous"' not in source
    assert "xvondMetaChannelSdkPromise = null" in source
    assert 'script.onerror = fail' in source


def test_meta_channel_settings_contract_is_channel_scoped():
    from pathlib import Path

    backend = Path("backend/app/api/customer_meta_channels.py").read_text(encoding="utf-8")
    frontend = Path("frontend/customer/meta-channels.js").read_text(encoding="utf-8")
    center = Path("frontend/customer/channel-center.js").read_text(encoding="utf-8")

    assert '@router.get("/settings")' in backend
    assert '@router.put("/settings")' in backend
    assert "channel.meta_customer_settings_updated" in backend
    assert '"tone": current.get("tone")' in backend
    assert '"response_style": current.get("response_style")' in backend
    assert '"response_length": current.get("response_length")' in backend
    assert "openCustomerMetaChannelSettings" in frontend
    assert "Channel-only instructions" in frontend
    assert "Channel settings" in center


def test_connected_meta_channel_auto_activates_when_ready(monkeypatch):
    channel = SimpleNamespace(company_id=11, enabled=False)

    class DummyDb:
        def flush(self):
            pass

    monkeypatch.setattr(module, "_ensure_channels_module", lambda db, company_id: None)
    monkeypatch.setattr(module, "_activation_blockers", lambda db, item: [])

    blockers = module._auto_activate_connected_channel(DummyDb(), channel)

    assert blockers == []
    assert channel.enabled is True


def test_connected_meta_channel_stays_disabled_when_blocked(monkeypatch):
    channel = SimpleNamespace(company_id=11, enabled=False)

    class DummyDb:
        def flush(self):
            pass

    monkeypatch.setattr(module, "_ensure_channels_module", lambda db, company_id: None)
    monkeypatch.setattr(module, "_activation_blockers", lambda db, item: ["blocked"])

    blockers = module._auto_activate_connected_channel(DummyDb(), channel)

    assert blockers == ["blocked"]
    assert channel.enabled is False
