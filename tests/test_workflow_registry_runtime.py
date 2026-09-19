from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts import workflow_registry_service as registry

SQL = Path("ops/n8n/idempotency.sql").read_text()
COMPOSE = Path("docker-compose.production.yml").read_text()
ENV_EXAMPLE = Path(".env.example").read_text()
SERVICE = Path("scripts/workflow_registry_service.py").read_text()


def _payload(**overrides):
    value = {
        "company_id": 1,
        "connection_key": "tenant-route-key-0001",
        "channel_id": 10,
        "agent_id": 20,
        "channel_type": "telegram",
        "provider_type": "telegram",
        "provider_url": "https://workflow.xvond.com/webhook/xvond-telegram-provider",
        "provider_secret": "s" * 32,
        "provider_config": {"bot_token": "secret"},
    }
    value.update(overrides)
    return value


def test_registry_secrets_are_encrypted_at_rest():
    assert "provider_secret_enc TEXT NOT NULL" in SQL
    assert "provider_config_enc TEXT NOT NULL" in SQL
    assert "DROP COLUMN IF EXISTS provider_secret" in SQL
    assert "DROP COLUMN IF EXISTS provider_config" in SQL
    assert "AESGCM" in SERVICE
    assert "_seal(payload.provider_secret" in SERVICE
    assert "_seal(payload.provider_config" in SERVICE


def test_legacy_plaintext_registry_rows_fail_closed_before_plaintext_columns_drop():
    assert "ADD COLUMN IF NOT EXISTS provider_secret_enc TEXT" in SQL
    assert "ADD COLUMN IF NOT EXISTS provider_config_enc TEXT" in SQL
    assert "SET active = FALSE" in SQL
    assert "provider_secret_enc IS NULL OR provider_config_enc IS NULL" in SQL


def test_registry_is_private_and_authenticated():
    assert "workflow-registry:" in COMPOSE
    section = COMPOSE.split("  workflow-registry:", 1)[1].split("  workflow-engine:", 1)[0]
    assert "ports:" not in section
    assert "WORKFLOW_REGISTRY_SHARED_SECRET" in section
    assert "WORKFLOW_PROVIDER_REGISTRY_KEY" in section
    assert "x_xvond_registry_secret" in SERVICE
    assert "compare_digest" in SERVICE


def test_compose_template_declares_registry_secrets():
    assert "WORKFLOW_REGISTRY_SHARED_SECRET=GENERATE_" in ENV_EXAMPLE
    assert "WORKFLOW_PROVIDER_REGISTRY_KEY=GENERATE_" in ENV_EXAMPLE


def test_route_identity_cannot_be_reassigned_by_upsert():
    assert "xvond_managed_channel_routes.channel_id=EXCLUDED.channel_id" in SERVICE
    assert "xvond_managed_channel_routes.agent_id=EXCLUDED.agent_id" in SERVICE
    assert "xvond_managed_channel_routes.channel_type=EXCLUDED.channel_type" in SERVICE
    assert "route_identity_conflict" in SERVICE


def test_registry_supports_explicit_deactivation():
    assert '@app.delete("/v1/routes/{company_id}/{connection_key}")' in SERVICE
    assert "SET active=FALSE" in SERVICE


def test_registry_crypto_roundtrip_is_bound_to_route_identity(monkeypatch):
    monkeypatch.setenv("WORKFLOW_PROVIDER_REGISTRY_KEY", "k" * 40)
    encrypted = registry._seal({"token": "provider-secret"}, b"1:route-a")
    assert "provider-secret" not in encrypted
    assert registry._open(encrypted, b"1:route-a") == {"token": "provider-secret"}
    with pytest.raises(Exception):
        registry._open(encrypted, b"1:route-b")


def test_registry_rejects_private_provider_urls():
    with pytest.raises(ValidationError):
        registry.Provision(**_payload(provider_url="https://127.0.0.1/provider"))
    with pytest.raises(ValidationError):
        registry.Provision(**_payload(provider_url="https://localhost/provider"))


def test_registry_enforces_provider_channel_pairing():
    with pytest.raises(ValidationError):
        registry.Provision(**_payload(provider_type="slack"))
    meta = registry.Provision(
        **_payload(
            channel_type="instagram",
            provider_type="meta",
            provider_url="https://workflow.xvond.com/webhook/xvond-meta-messaging-provider",
        )
    )
    assert meta.provider_type == "meta"
    assert meta.channel_type == "instagram"


def test_registry_bounds_provider_config_size():
    with pytest.raises(ValidationError):
        registry.Provision(**_payload(provider_config={"blob": "x" * 70_000}))
