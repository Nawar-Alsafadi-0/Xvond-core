import json
from pathlib import Path

from backend.app.modules.channels.catalog import (
    get_channel_capability,
    packaged_managed_channel_types,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = json.loads(
    (ROOT / "ops" / "n8n" / "xvond-custom-channel-provider.workflow.json")
    .read_text(encoding="utf-8")
)
SCRIPT = (ROOT / "scripts" / "validate_custom_channel_route.sh").read_text(
    encoding="utf-8"
)
SYNC = (ROOT / "scripts" / "sync_workflow_engine.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def _node(name: str) -> dict:
    return next(node for node in WORKFLOW["nodes"] if node["name"] == name)


def test_custom_channel_is_packaged_on_shared_managed_runtime():
    capability = get_channel_capability("custom")
    assert capability["runtime_state"] == "live"
    assert capability["setup_mode"] == "managed"
    assert capability["runtime_adapter"] == "n8n_channel_gateway"
    assert capability["packaged_provider"] is True
    assert "custom" in packaged_managed_channel_types()


def test_custom_inbound_is_signed_replay_bounded_and_deduplicable():
    inbound = _node("Custom Channel Inbound")
    assert inbound["parameters"]["options"]["rawBody"] is True

    code = _node("Verify and Normalize Custom Channel")["parameters"]["jsCode"]
    assert "x-xvond-custom-timestamp" in code.lower()
    assert "x-xvond-custom-signature" in code.lower()
    assert "createHmac('sha256'" in code
    assert "timingSafeEqual" in code
    assert "Math.abs(now - requestTime) > 300" in code
    assert "inboundSecret.length < 32" in code
    assert "external_message_id" in code
    assert "external_contact_id" in code
    assert "channel_type:'custom'" in code


def test_custom_inbound_acknowledges_before_forwarding_to_xvond():
    connections = WORKFLOW["connections"]
    assert (
        connections["Verify and Normalize Custom Channel"]["main"][0][0]["node"]
        == "Respond to Custom Sender"
    )
    assert (
        connections["Respond to Custom Sender"]["main"][0][0]["node"]
        == "Forward Custom Message?"
    )
    forward = _node("Forward Custom Message to Xvond")
    assert forward["parameters"]["url"] == "={{ $env.XVOND_INTERNAL_CHANNEL_URL }}"
    headers = forward["parameters"]["headerParameters"]["parameters"]
    assert any(item["name"] == "X-Xvond-N8N-Secret" for item in headers)


def test_custom_outbound_requires_https_idempotency_and_provider_confirmation():
    validate = _node("Validate Custom Send")["parameters"]["jsCode"]
    assert "parsed.protocol !== 'https:'" in validate
    assert "parsed.username || parsed.password" in validate
    assert "localhost" in validate
    assert "idempotencyKey" in validate
    assert "provider_secret" in validate
    assert "outbound_secret" in validate
    assert "length < 32" in validate

    send = _node("Send Custom Message")
    headers = {
        item["name"]: item["value"]
        for item in send["parameters"]["headerParameters"]["parameters"]
    }
    assert "X-Xvond-Custom-Secret" in headers
    assert "X-Xvond-Request-ID" in headers
    assert "Idempotency-Key" in headers

    normalize = _node("Normalize Custom Send Result")["parameters"]["jsCode"]
    assert "response.success !== true" in normalize
    assert "provider_message_id" in normalize
    assert "Custom channel endpoint did not confirm delivery" in normalize


def test_custom_route_validator_is_structural_and_never_makes_provider_side_effect():
    assert 'exec -T workflow-engine' in SCRIPT
    assert "XVOND_CUSTOM_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "xvond-custom-channel-provider" in SCRIPT
    assert "outbound_url must be credential-free HTTPS" in SCRIPT
    assert "route secrets must be at least 32 characters" in SCRIPT
    assert "provider secret mismatch" in SCRIPT
    assert "fetch(" not in SCRIPT


def test_custom_route_validator_never_prints_provider_secrets():
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "inbound_secret:" not in printed
    assert "outbound_secret:" not in printed
    assert "provider_secret:" not in printed


def test_release_sync_imports_and_probes_custom_channel_workflow():
    assert "xvond-custom-channel-provider.workflow.json" in SYNC
    assert (
        'CUSTOM_CHANNEL_WORKFLOW_ID="${CUSTOM_CHANNEL_WORKFLOW_ID:-xvond-custom-channel-provider-v1}"'
        in SYNC
    )
    assert "probe_custom_channel_gateway" in SYNC
    assert (
        'sync_one_workflow "$CUSTOM_CHANNEL_WORKFLOW_FILE" "$CUSTOM_CHANNEL_WORKFLOW_ID"'
        in SYNC
    )


def test_workflow_container_receives_custom_route_registry():
    assert "XVOND_CUSTOM_CHANNEL_ROUTES_JSON=" in ENV
    assert (
        "XVOND_CUSTOM_CHANNEL_ROUTES_JSON: ${XVOND_CUSTOM_CHANNEL_ROUTES_JSON:-{}}"
        in COMPOSE
    )
