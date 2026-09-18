import json
from pathlib import Path


WORKFLOW_PATH = Path("ops/n8n/xvond-actions.workflow.json")
CONTRACTS_PATH = Path("ops/n8n/action-contracts.json")
CHANNEL_INBOUND_PATH = Path("ops/n8n/xvond-channel-inbound.workflow.json")


def _workflow_code() -> str:
    payload = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}
    return nodes["Validate and Dispatch"]["parameters"]["jsCode"]


def test_workflow_template_is_valid_and_uses_expected_webhook_contract():
    payload = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert payload["name"] == "Xvond Actions Gateway"
    nodes = {node["name"]: node for node in payload["nodes"]}
    webhook = nodes["Xvond Webhook"]
    assert webhook["parameters"]["httpMethod"] == "POST"
    assert webhook["parameters"]["path"] == "xvond-actions"
    assert webhook["parameters"]["responseMode"] == "responseNode"
    code = _workflow_code()
    assert "N8N_SHARED_SECRET" in code
    assert "health_check" in code
    assert "request_id" in code
    assert "company_id" in code
    assert "agent_id" in code
    assert "action" in code
    assert "provider_not_configured" in code
    assert "missing_idempotency_key" in code
    assert "Return to Xvond" in nodes


def test_registered_actions_are_declared_in_master_workflow():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    code = _workflow_code()
    for action_name in contracts["actions"]:
        assert action_name in code, f"{action_name} is missing from master workflow routing"


def test_mutating_actions_require_idempotency():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    assert contracts["policy"]["mutating_actions_require_idempotency"] is True
    for name, spec in contracts["actions"].items():
        if spec.get("side_effect"):
            assert "idempotency_key" in spec.get("required_data", []), name


def test_workflow_fail_closed_policy_is_explicit():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    policy = contracts["policy"]
    assert policy["provider_success_required_before_success_true"] is True
    assert policy["credentials_live_in_workflow_engine"] is True
    assert policy["xvond_database_access_from_workflows"] is False


def test_generic_employee_actions_are_supported_by_contract_and_gateway():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    generic = contracts["generic_business_action"]
    code = _workflow_code()
    assert generic["adapter"] == "business"
    assert generic["pattern"] == "<action_key>.(check_availability|execute|cancel)"
    assert contracts["policy"]["generic_employee_actions_are_config_driven"] is True
    assert "check_availability|execute|cancel" in code
    assert "action_config" in code
    assert "declaredActionType !== actionKey" in code
    assert "moduleName" in code
    assert "destination_type" in code


def test_generic_mutating_operations_require_stable_idempotency():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    generic = contracts["generic_business_action"]
    assert generic["mutating_operations"] == ["execute", "cancel"]
    assert generic["mutating_operations_require_idempotency"] is True
    code = _workflow_code()
    assert "operation !== 'check_availability'" in code
    assert "route.mutates && !idempotencyKey" in code


def test_generic_action_key_is_not_hardcoded_to_business_templates():
    code = _workflow_code()
    assert "action.match" in code
    assert "action_key: actionKey" in code
    assert "Unsupported workflow action" in code


def test_universal_channel_send_is_routed_by_xvond_connection_key():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    code = _workflow_code()
    channel_check = contracts["actions"]["channel.check"]
    channel = contracts["actions"]["channel.send"]

    assert channel_check["adapter"] == "channel"
    assert channel_check["side_effect"] is False
    assert "connection_key" in channel_check["required_data"]
    assert "channel.check" in code
    assert channel["adapter"] == "channel"
    assert channel["side_effect"] is True
    assert "connection_key" in channel["required_data"]
    assert "external_contact_id" in channel["required_data"]
    assert "XVOND_CHANNEL_ROUTES_JSON" in code
    assert "channel_provider" in code
    assert "channel.send" in code
    assert contracts["policy"]["channel_credentials_live_in_workflow_engine"] is True
    assert contracts["policy"]["channel_routes_are_tenant_scoped"] is True


def test_inbound_channel_gateway_calls_xvond_then_provider_without_exposing_credentials():
    payload = json.loads(CHANNEL_INBOUND_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}

    webhook = nodes["Xvond Channel Inbound"]
    assert webhook["parameters"]["path"] == "xvond-channel-inbound"
    validate = nodes["Validate Normalized Message"]["parameters"]["jsCode"]
    prepare = nodes["Prepare Channel Reply"]["parameters"]["jsCode"]

    assert "N8N_SHARED_SECRET" in validate
    assert "external_contact_id" in validate
    assert "external_message_id" in validate
    assert "XVOND_INTERNAL_CHANNEL_URL" in str(nodes["Run Xvond Employee"]["parameters"])
    assert "XVOND_CHANNEL_ROUTES_JSON" in prepare
    assert "channel-reply:" in prepare
    assert "provider_secret" not in prepare
    assert "XVOND_CHANNEL_ROUTES_JSON" in str(nodes["Send Channel Reply"]["parameters"])
    assert "Send Channel Reply" in nodes
    assert "Provider Delivery Confirmed?" in nodes
    assert "Confirm Channel Delivery" in nodes
    assert "XVOND_INTERNAL_CHANNEL_CONFIRM_URL" in str(
        nodes["Confirm Channel Delivery"]["parameters"]
    )
    confirm_body = str(nodes["Confirm Channel Delivery"]["parameters"])
    assert "conversation_id" in confirm_body
    assert "response_message_id" in confirm_body
    assert "provider_message_id" in confirm_body
