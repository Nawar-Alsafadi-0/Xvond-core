import json
from pathlib import Path

from backend.app.modules.channels.catalog import (
    get_channel_capability,
    packaged_managed_channel_types,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "ops" / "n8n" / "xvond-microsoft-teams-provider.workflow.json"
WORKFLOW = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
SCRIPT = (ROOT / "scripts" / "validate_microsoft_teams_route.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "scripts" / "sync_workflow_engine.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def _node(name: str) -> dict:
    return next(node for node in WORKFLOW["nodes"] if node["name"] == name)


def test_teams_is_a_packaged_managed_channel():
    capability = get_channel_capability("teams")
    assert capability["runtime_state"] == "live"
    assert capability["setup_mode"] == "managed"
    assert capability["runtime_adapter"] == "n8n_channel_gateway"
    assert capability["packaged_provider"] is True
    assert "teams" in packaged_managed_channel_types()


def test_teams_inbound_fully_verifies_bot_connector_jwt():
    code = _node("Verify and Normalize Microsoft Teams")["parameters"]["jsCode"]
    assert "login.botframework.com/v1/.well-known/openidconfiguration" in code
    assert "https://api.botframework.com" in code
    assert "claims.aud" in code
    assert "claims.serviceurl" in code
    assert "crypto.createPublicKey" in code
    assert "jwk.endorsements" in code
    assert "endorsements.includes(channelId)" in code
    assert "channel_endorsement_missing" in code
    assert "_http_status:403" in code
    assert "crypto.verify('RSA-SHA256'" in code
    assert "Number(claims.exp" in code
    assert "Number(claims.nbf" in code
    assert "channelId" in code
    assert "msteams" in code
    assert "channel_type:'teams'" in code


def test_teams_inbound_acks_before_forwarding_to_xvond():
    connections = WORKFLOW["connections"]
    assert connections["Verify and Normalize Microsoft Teams"]["main"][0][0]["node"] == "Respond to Microsoft Teams"
    assert connections["Respond to Microsoft Teams"]["main"][0][0]["node"] == "Forward Teams?"
    forward = _node("Forward Teams to Xvond")
    assert forward["parameters"]["url"] == "={{ $env.XVOND_INTERNAL_CHANNEL_URL }}"
    headers = forward["parameters"]["headerParameters"]["parameters"]
    assert any(item["name"] == "X-Xvond-N8N-Secret" for item in headers)


def test_teams_outbound_uses_bot_framework_oauth_and_requires_activity_id():
    code = _node("Send Microsoft Teams Message")["parameters"]["jsCode"]
    assert "microsoft_tenant_id" in code
    assert "'botframework.com'" in code
    assert "'https://login.microsoftonline.com/' + encodeURIComponent(tenantId)" in code
    assert "tenantGuid.test(tenantId)" in code
    assert "https://api.botframework.com/.default" in code
    assert "'/v3/conversations/'" in code
    assert "'/activities'" in code
    assert "payload?.id" in code
    assert "provider_message_id:providerId" in code


def test_teams_route_validator_checks_registries_oauth_and_never_prints_secrets():
    assert "XVOND_MICROSOFT_TEAMS_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "xvond-microsoft-teams-provider" in SCRIPT
    assert "microsoft_tenant_id" in SCRIPT
    assert "'https://login.microsoftonline.com/' + encodeURIComponent(tenantId)" in SCRIPT
    assert "tenantGuid.test(tenantId)" in SCRIPT
    assert "oauth_tenant_mode" in SCRIPT
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "microsoft_app_password:" not in printed
    assert "provider_secret:" not in printed


def test_release_sync_imports_and_probes_teams_workflow():
    assert "xvond-microsoft-teams-provider.workflow.json" in SYNC
    assert 'MICROSOFT_TEAMS_WORKFLOW_ID="${MICROSOFT_TEAMS_WORKFLOW_ID:-xvond-microsoft-teams-provider-v1}"' in SYNC
    assert "probe_microsoft_teams_gateway" in SYNC
    assert 'sync_one_workflow "$MICROSOFT_TEAMS_WORKFLOW_FILE" "$MICROSOFT_TEAMS_WORKFLOW_ID"' in SYNC


def test_workflow_container_receives_teams_route_registry():
    assert "XVOND_MICROSOFT_TEAMS_ROUTES_JSON=" in ENV
    assert "microsoft_tenant_id" in ENV
    assert "single-tenant" in ENV
    assert "XVOND_MICROSOFT_TEAMS_ROUTES_JSON: ${XVOND_MICROSOFT_TEAMS_ROUTES_JSON:-{}}" in COMPOSE
