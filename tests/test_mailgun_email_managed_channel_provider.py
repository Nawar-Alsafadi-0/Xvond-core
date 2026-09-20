import json
from pathlib import Path

from backend.app.modules.channels.catalog import (
    get_channel_capability,
    packaged_managed_channel_types,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "ops" / "n8n" / "xvond-mailgun-email-provider.workflow.json"
WORKFLOW = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
SCRIPT = (ROOT / "scripts" / "validate_mailgun_email_route.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "scripts" / "sync_workflow_engine.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def _node(name: str) -> dict:
    return next(node for node in WORKFLOW["nodes"] if node["name"] == name)


def test_email_is_a_packaged_managed_channel():
    capability = get_channel_capability("email")
    assert capability["runtime_state"] == "live"
    assert capability["setup_mode"] == "managed"
    assert capability["runtime_adapter"] == "n8n_channel_gateway"
    assert capability["packaged_provider"] is True
    assert "email" in packaged_managed_channel_types()


def test_mailgun_inbound_verifies_hmac_and_normalizes_email():
    code = _node("Verify and Normalize Mailgun Email")["parameters"]["jsCode"]
    assert "XVOND_MAILGUN_EMAIL_ROUTES_JSON" in code
    assert "createHmac('sha256'" in code
    assert "timestamp + token" in code
    assert "timingSafeEqual" in code
    assert "Math.abs(now-requestTime) > 900" in code
    assert "body['body-plain']" in code
    assert "channel_type:'email'" in code
    assert "external_contact_id:sender" in code
    assert ".slice(0, 200)" in code
    assert ".slice(0, 180)" in code
    assert ".slice(0, 12000)" in code


def test_mailgun_inbound_acks_before_forwarding_to_xvond():
    connections = WORKFLOW["connections"]
    assert connections["Verify and Normalize Mailgun Email"]["main"][0][0]["node"] == "Respond to Mailgun"
    assert connections["Respond to Mailgun"]["main"][0][0]["node"] == "Forward Email?"
    forward = _node("Forward Email to Xvond")
    assert forward["parameters"]["url"] == "={{ $env.XVOND_INTERNAL_CHANNEL_URL }}"
    headers = forward["parameters"]["headerParameters"]["parameters"]
    assert any(item["name"] == "X-Xvond-N8N-Secret" for item in headers)


def test_mailgun_outbound_requires_provider_message_identity():
    code = _node("Send Mailgun Email")["parameters"]["jsCode"]
    assert "https://api.eu.mailgun.net" in code
    assert "https://api.mailgun.net" in code
    assert "'/v3/' + encodeURIComponent(domain) + '/messages'" in code
    assert "payload?.id" in code
    assert "provider_message_id:providerId" in code
    assert "v:xvond_idempotency_key" in code


def test_mailgun_route_validator_checks_registries_and_provider():
    assert "XVOND_MAILGUN_EMAIL_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "xvond-mailgun-email-provider" in SCRIPT
    assert "'/v3/domains/'" in SCRIPT
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "api_key:" not in printed
    assert "webhook_signing_key:" not in printed
    assert "provider_secret:" not in printed


def test_release_sync_imports_and_probes_mailgun_email_workflow():
    assert "xvond-mailgun-email-provider.workflow.json" in SYNC
    assert 'MAILGUN_EMAIL_WORKFLOW_ID="${MAILGUN_EMAIL_WORKFLOW_ID:-xvond-mailgun-email-provider-v1}"' in SYNC
    assert "probe_mailgun_email_gateway" in SYNC
    assert 'sync_one_workflow "$MAILGUN_EMAIL_WORKFLOW_FILE" "$MAILGUN_EMAIL_WORKFLOW_ID"' in SYNC


def test_workflow_container_receives_mailgun_route_registry():
    assert "XVOND_MAILGUN_EMAIL_ROUTES_JSON=" in ENV
    assert "XVOND_MAILGUN_EMAIL_ROUTES_JSON: ${XVOND_MAILGUN_EMAIL_ROUTES_JSON:-{}}" in COMPOSE
