from pathlib import Path

SQL = Path("ops/n8n/idempotency.sql").read_text()
COMPOSE = Path("docker-compose.production.yml").read_text()
SERVICE = Path("scripts/workflow_registry_service.py").read_text()


def test_registry_secrets_are_encrypted_at_rest():
    assert "provider_secret_enc TEXT NOT NULL" in SQL
    assert "provider_config_enc TEXT NOT NULL" in SQL
    assert "provider_secret TEXT NOT NULL" not in SQL
    assert "AESGCM" in SERVICE
    assert "_seal(payload.provider_secret" in SERVICE
    assert "_seal(payload.provider_config" in SERVICE


def test_registry_is_private_and_authenticated():
    assert "workflow-registry:" in COMPOSE
    section = COMPOSE.split("  workflow-registry:", 1)[1].split("  workflow-engine:", 1)[0]
    assert "ports:" not in section
    assert "WORKFLOW_REGISTRY_SHARED_SECRET" in section
    assert "WORKFLOW_PROVIDER_REGISTRY_KEY" in section
    assert "x_xvond_registry_secret" in SERVICE
    assert "compare_digest" in SERVICE


def test_route_identity_cannot_be_reassigned_by_upsert():
    assert "WHERE xvond_managed_channel_routes.channel_id=EXCLUDED.channel_id" in SERVICE
    assert "route_identity_conflict" in SERVICE


def test_registry_supports_explicit_deactivation():
    assert '@app.delete("/v1/routes/{company_id}/{connection_key}")' in SERVICE
    assert "SET active=FALSE" in SERVICE
