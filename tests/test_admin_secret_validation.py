from backend.app.api.admin_channels import serialize_channel
from backend.app.api.admin_integrations import serialize_integration
from backend.app.core.config_secrets import ENCRYPTED_PREFIX
from backend.app.modules.channels.models import AgentChannel
from backend.app.modules.integrations.models import CompanyIntegration


def test_encrypted_whatsapp_channel_is_reported_configured():
    channel = AgentChannel(
        company_id=1,
        agent_id=1,
        channel_type="whatsapp",
        config={
            "phone_number_id": "123",
            "access_token": "access",
            "verify_token": "verify",
            "app_secret": "secret",
            "graph_api_version": "v23.0",
        },
    )

    assert channel.config["access_token"].startswith(ENCRYPTED_PREFIX)
    result = serialize_channel(channel)
    assert result["configured"] is True
    assert result["connected"] is False
    assert result["connection_status"] == "configured_only"
    assert result["meta_onboarding_complete"] is False
    assert "access_token" not in result["config"]


def test_encrypted_integration_is_validated_and_redacted():
    item = CompanyIntegration(
        company_id=1,
        integration_type="crm",
        name="CRM",
        config={
            "base_url": "https://example.com",
            "api_key": "secret",
            "validation_endpoint": "/me",
        },
    )

    assert item.config["api_key"].startswith(ENCRYPTED_PREFIX)
    result = serialize_integration(item)
    assert result["configured"] is True
    assert result["config"] == {
        "base_url": "https://example.com",
        "validation_endpoint": "/me",
    }
    assert result["configured_secret_fields"] == ["api_key"]
