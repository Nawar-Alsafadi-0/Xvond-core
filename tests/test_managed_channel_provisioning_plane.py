import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = json.loads((ROOT / "ops" / "n8n" / "xvond-actions.workflow.json").read_text())
SQL = (ROOT / "ops" / "n8n" / "idempotency.sql").read_text()
ADMIN = (ROOT / "backend" / "app" / "api" / "admin_channels.py").read_text()
GATEWAY = (ROOT / "backend" / "app" / "core" / "n8n_gateway.py").read_text()


def _node(name):
    return next(node for node in WORKFLOW["nodes"] if node["name"] == name)


def test_workflow_plane_owns_managed_channel_route_registry():
    assert "CREATE TABLE IF NOT EXISTS xvond_managed_channel_routes" in SQL
    assert "provider_secret_enc TEXT NOT NULL" in SQL
    assert "provider_config_enc TEXT NOT NULL" in SQL
    assert "PRIMARY KEY (company_id, connection_key)" in SQL
    assert "UNIQUE (channel_id)" in SQL


def test_admin_connect_can_provision_without_persisting_provider_credentials():
    assert "provider_config: dict | None = None" in ADMIN
    assert "n8n_gateway.provision_channel(" in ADMIN
    connect_tail = ADMIN.split('def connect_managed_channel(', 1)[1].split('@router.get("/{channel_id}/readiness")', 1)[0]
    assert '"provider_secret"' not in connect_tail.split("incoming = {", 1)[1]
    assert '"provider_config"' not in connect_tail.split("incoming = {", 1)[1]


def test_gateway_uses_one_shot_nonretrying_provision_action():
    block = GATEWAY.split("def provision_channel(", 1)[1].split("def execute(", 1)[0]
    assert 'action="channel.provision"' in block
    assert "max_retries_override=0" in block


def test_actions_workflow_provisions_and_resolves_routes_from_registry():
    code = _node("Validate and Dispatch")["parameters"]["jsCode"]
    assert "'channel.provision'" in code
    assert "_dispatch: 'channel_provision'" in code
    assert "_dispatch: 'channel_registry_lookup'" in code
    assert "allowedProviders" in code
    assert "providerSecret.length < 32" in code

    provision = _node("Provision Channel Route")["parameters"]
    assert "XVOND_WORKFLOW_REGISTRY_URL" in provision["url"]
    assert "/v1/routes" in provision["url"]
    assert "XVOND_WORKFLOW_REGISTRY_SECRET" in str(provision["headerParameters"])

    lookup = _node("Lookup Channel Route")["parameters"]
    assert "XVOND_WORKFLOW_REGISTRY_URL" in lookup["url"]
    assert "/v1/routes/" in lookup["url"]
    assert "XVOND_WORKFLOW_REGISTRY_SECRET" in str(lookup["headerParameters"])


def test_channel_provider_uses_registry_result_not_env_route_json():
    execute = _node("Execute Channel Provider")
    assert execute["parameters"]["url"] == "={{ $json.provider_url }}"
    headers = execute["parameters"]["headerParameters"]["parameters"]
    secret = next(x for x in headers if x["name"] == "X-Xvond-Channel-Secret")
    assert secret["value"] == "={{ $json.provider_secret }}"

    lookup_code = _node("Normalize Channel Route Lookup")["parameters"]["jsCode"]
    assert "provider_url:String(row.provider_url)" in lookup_code
    assert "provider_secret:String(row.provider_secret)" in lookup_code

def test_gateway_uses_one_shot_route_deactivation_action():
    block = GATEWAY.split("def deactivate_channel(", 1)[1].split("def execute(", 1)[0]
    assert 'action="channel.deactivate"' in block
    assert "max_retries_override=0" in block


def test_actions_workflow_deactivates_routes_through_private_registry():
    code = _node("Validate and Dispatch")["parameters"]["jsCode"]
    assert "'channel.deactivate'" in code
    assert "_dispatch: 'channel_deactivate'" in code

    gate = _node("Channel Deactivate?")["parameters"]
    assert "channel_deactivate" in str(gate)

    deactivate = _node("Deactivate Channel Route")["parameters"]
    assert deactivate["method"] == "DELETE"
    assert "XVOND_WORKFLOW_REGISTRY_URL" in deactivate["url"]
    assert "/v1/routes/" in deactivate["url"]
    assert "XVOND_WORKFLOW_REGISTRY_SECRET" in str(deactivate["headerParameters"])

    normalized = _node("Normalize Deactivate Result")["parameters"]["jsCode"]
    assert "deactivated" in normalized
    assert "Managed channel deactivation failed" in normalized
