import json
from pathlib import Path


WORKFLOW_PATH = Path("ops/n8n/xvond-channels.workflow.json")


def workflow_code() -> str:
    payload = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}
    return nodes["Validate Channel Route"]["parameters"]["jsCode"]


def test_managed_channel_workflow_uses_xvond_contract_and_hidden_registry():
    payload = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert payload["name"] == "Xvond Managed Channels Gateway"
    nodes = {node["name"]: node for node in payload["nodes"]}
    webhook = nodes["Xvond Channels Webhook"]
    assert webhook["parameters"]["httpMethod"] == "POST"
    assert webhook["parameters"]["path"] == "xvond-channels"

    code = workflow_code()
    assert "N8N_SHARED_SECRET" in code
    assert "XVOND_WORKFLOW_CHANNELS_JSON" in code
    assert "channel.check" in code
    assert "channel.send" in code
    assert "idempotency_key" in code
    assert "provider_not_configured" in code
    assert "startsWith('https://')" in code
    assert "_dispatch: 'send'" in code


def test_managed_channel_provider_dispatch_keeps_credentials_in_workflow_plane():
    payload = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}
    sender = nodes["Execute Channel Route"]["parameters"]

    assert sender["url"] == "={{ $json.route_url }}"
    headers = sender["headerParameters"]["parameters"]
    names = {item["name"] for item in headers}
    assert "X-Xvond-Channel-Secret" in names
    assert "Idempotency-Key" in names
    assert "X-Xvond-Request-ID" in names
    assert "route_secret" in str(headers)
    assert "$json.action" in sender["body"]
    assert "access_token" not in json.dumps(payload).lower()
    assert "bot_token" not in json.dumps(payload).lower()


def test_managed_channel_result_is_normalized_before_returning_to_core():
    payload = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}
    code = nodes["Normalize Channel Result"]["parameters"]["jsCode"]

    assert "provider_message_id" in code
    assert "provider_check_failed" in code
    assert "Managed channel provider check failed" in code
    assert "Managed channel provider delivery failed" in code
    assert "provider.stack" not in code
