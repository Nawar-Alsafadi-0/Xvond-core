import json
from pathlib import Path

from backend.app.modules.channels.catalog import (
    get_channel_capability,
    packaged_managed_channel_types,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = json.loads(
    (ROOT / "ops" / "n8n" / "xvond-twilio-sms-provider.workflow.json")
    .read_text(encoding="utf-8")
)
SCRIPT = (ROOT / "scripts" / "validate_twilio_sms_route.sh").read_text(
    encoding="utf-8"
)
SYNC = (ROOT / "scripts" / "sync_workflow_engine.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
ENV = (ROOT / ".env.example").read_text(encoding="utf-8")


def _node(name: str) -> dict:
    return next(node for node in WORKFLOW["nodes"] if node["name"] == name)


def test_sms_is_packaged_on_shared_managed_runtime():
    capability = get_channel_capability("sms")
    assert capability["runtime_state"] == "live"
    assert capability["setup_mode"] == "managed"
    assert capability["runtime_adapter"] == "n8n_channel_gateway"
    assert capability["packaged_provider"] is True
    assert "sms" in packaged_managed_channel_types()


def test_twilio_inbound_signature_uses_exact_url_and_all_form_fields():
    code = _node("Verify and Normalize Twilio SMS")["parameters"]["jsCode"]
    assert "x-twilio-signature" in code.lower()
    assert "route.inbound_url" in code
    assert "Object.keys(body).sort()" in code
    assert "signingPayload += key + String" in code
    assert "createHmac('sha1'" in code
    assert "digest('base64')" in code
    assert "timingSafeEqual" in code
    assert "MessageSid" in code
    assert "AccountSid" in code
    assert "From" in code
    assert "To" in code
    assert "Body" in code
    assert "channel_type:'sms'" in code


def test_twilio_inbound_acks_with_twiml_before_forwarding():
    connections = WORKFLOW["connections"]
    assert (
        connections["Verify and Normalize Twilio SMS"]["main"][0][0]["node"]
        == "Respond to Twilio"
    )
    assert (
        connections["Respond to Twilio"]["main"][0][0]["node"]
        == "Forward SMS?"
    )
    respond = _node("Respond to Twilio")
    assert respond["parameters"]["responseBody"] == "={{ $json._response }}"
    assert any(
        entry["name"] == "Content-Type" and entry["value"] == "text/xml"
        for entry in respond["parameters"]["options"]["responseHeaders"]["entries"]
    )
    forward = _node("Forward SMS to Xvond")
    assert forward["parameters"]["url"] == "={{ $env.XVOND_INTERNAL_CHANNEL_URL }}"


def test_twilio_outbound_uses_messages_api_and_requires_sid():
    code = _node("Send Twilio SMS")["parameters"]["jsCode"]
    assert "https://api.twilio.com/2010-04-01/Accounts/" in code
    assert "/Messages.json" in code
    assert "'Authorization':'Basic ' + basic" in code
    assert "application/x-www-form-urlencoded" in code
    assert "form.set('To', to)" in code
    assert "form.set('Body', message)" in code
    assert "form.set('From', fromNumber)" in code
    assert "form.set('MessagingServiceSid', messagingServiceSid)" in code
    assert "const sid = String(payload?.sid || '').trim()" in code
    assert "provider_message_id:sid" in code


def test_twilio_route_validator_pins_exact_signed_url_and_one_sender():
    assert "XVOND_TWILIO_SMS_ROUTES_JSON" in SCRIPT
    assert "XVOND_CHANNEL_ROUTES_JSON" in SCRIPT
    assert "xvond-twilio-sms-provider" in SCRIPT
    assert "xvond-twilio-sms-inbound" in SCRIPT
    assert "configure exactly one sender" in SCRIPT
    assert "inbound_url must be the exact signed Xvond Twilio webhook URL" in SCRIPT
    assert "provider secret mismatch" in SCRIPT
    assert "account_sid format is invalid" in SCRIPT


def test_twilio_route_validator_never_prints_secrets():
    printed = SCRIPT.split("console.log(JSON.stringify({", 1)[1]
    assert "auth_token:" not in printed
    assert "provider_secret:" not in printed


def test_release_sync_imports_and_probes_twilio_sms_workflow():
    assert "xvond-twilio-sms-provider.workflow.json" in SYNC
    assert (
        'TWILIO_SMS_WORKFLOW_ID="${TWILIO_SMS_WORKFLOW_ID:-xvond-twilio-sms-provider-v1}"'
        in SYNC
    )
    assert "probe_twilio_sms_gateway" in SYNC
    assert (
        'sync_one_workflow "$TWILIO_SMS_WORKFLOW_FILE" "$TWILIO_SMS_WORKFLOW_ID"'
        in SYNC
    )


def test_workflow_container_receives_twilio_route_registry():
    assert "XVOND_TWILIO_SMS_ROUTES_JSON=" in ENV
    assert (
        "XVOND_TWILIO_SMS_ROUTES_JSON: ${XVOND_TWILIO_SMS_ROUTES_JSON:-{}}"
        in COMPOSE
    )
