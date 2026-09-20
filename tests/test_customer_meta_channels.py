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
