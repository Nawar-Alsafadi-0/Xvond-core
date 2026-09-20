import json
from pathlib import Path

from backend.app.modules.channels.catalog import (
    get_channel_capability,
    packaged_managed_channel_types,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "ops" / "n8n" / "xvond-slack-provider.workflow.json"
WORKFLOW = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
SCRIPT = (ROOT / "scripts" / "validate_slack_channel_route.sh").read_text(encoding="utf-8")
SYNC = (ROOT / "scripts" / "sync_workflow_engine.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def _node(name: str) -> dict:
    return next(node for node in WORKFLOW["nodes"] if node["name"] == name)


def test_slack_is_a_packaged_managed_channel():
    capability = get_channel_capability("slack")
    assert capability["runtime_state"] == "live"
    assert capability["setup_mode"] == "managed"
    assert capability["runtime_adapter"] == "n8n_channel_gateway"
    assert capability["packaged_provider"] is True
    assert "slack" in packaged_managed_channel_types()


def test_slack_inbound_uses_raw_body_signature_and_replay_protection():
    inbound = _node("Slack Inbound")
    assert inbound["parameters"]["options"]["rawBody"] is True

    code = _node("Verify and Normalize Slack")["parameters"]["jsCode"]
    assert "x-slack-signature" in code.lower()
    assert "x-slack-request-timestamp" in code.lower()
    assert "v0:" in code
    assert "createHmac('sha256'" in code
    assert "timingSafeEqual" in code
    assert "Math.abs(now - requestTime) > 300" in code
    assert "url_verification" in code
    assert "event_callback" in code
    assert "event_id" in code
    assert "bot_id" in code
    assert "channel_type:'slack'" in code


def test_slack_acks_before_forwarding_to_xvond():
    connections = WORKFLOW["connections"]
    assert connections["Verify and Normalize Slack"]["main"][0][0]["node"] == "Respond to Slack"
    assert connections["Respond to Slack"]["main"][0][0]["node"] == "Forward Slack?"
    forward = _node("Forward Slack to Xvond")
    assert forward["parameters"]["url"] == "={{ $env.XVOND_INTERNAL_CHANNEL_URL }}"
    headers = forward["parameters"]["headerParameters"]["parameters"]
    assert any(item["name"] == "X-Xvond-N8N-Secret" for item in headers)


def test_slack_outbound_uses_web_api_and_requires_provider_confirmation():
    send = _node("Slack chat.postMessage")
    code = send["parameters"]["jsCode"]
    assert "https://slack.com/api/chat.postMessage" in code
    assert "XVOND_WORKFLOW_REGISTRY_URL" in code
    assert "provider_config?.bot_token" in code
    assert "'Authorization':'Bearer ' + botToken" in code

    normalize = _node("Normalize Slack Send Result")["parameters"]["jsCode"]
    assert "response.ok !== true" in normalize
    assert "!response.ts" in normalize
    assert "provider_message_id:String(response.ts)" in normalize


def test_slack_route_validator_checks_both_registries_and_provider_identity():
    assert 'exec -T workflow-engine' in SCRIPT
    assert "XVOND_SLACK_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "xvond-slack-provider" in SCRIPT
    assert "https://slack.com/api/auth.test" in SCRIPT
    assert "provider secret mismatch" in SCRIPT
    assert "configured team_id does not match Slack" in SCRIPT


def test_slack_route_validator_never_prints_provider_credentials():
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "bot_token:" not in printed
    assert "signing_secret:" not in printed
    assert "provider_secret:" not in printed


def test_release_sync_imports_and_probes_slack_workflow():
    assert "xvond-slack-provider.workflow.json" in SYNC
    assert 'SLACK_WORKFLOW_ID="${SLACK_WORKFLOW_ID:-xvond-slack-provider-v1}"' in SYNC
    assert "probe_slack_gateway" in SYNC
    assert 'sync_one_workflow "$SLACK_WORKFLOW_FILE" "$SLACK_WORKFLOW_ID"' in SYNC


def test_workflow_container_receives_slack_route_registry():
    assert "XVOND_SLACK_ROUTES_JSON=" in ENV
    assert "XVOND_SLACK_ROUTES_JSON: ${XVOND_SLACK_ROUTES_JSON:-{}}" in COMPOSE
