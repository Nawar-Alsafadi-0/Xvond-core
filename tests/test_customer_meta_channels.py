from backend.app.api.customer_meta_channels import _normalize_channel_type, _public_asset
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
