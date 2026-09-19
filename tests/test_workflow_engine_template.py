import json
from pathlib import Path


WORKFLOW_PATH = Path("ops/n8n/xvond-actions.workflow.json")
CONTRACTS_PATH = Path("ops/n8n/action-contracts.json")
CHANNEL_INBOUND_PATH = Path("ops/n8n/xvond-channel-inbound.workflow.json")
TELEGRAM_PROVIDER_PATH = Path("ops/n8n/xvond-telegram-provider.workflow.json")
META_PROVIDER_PATH = Path("ops/n8n/xvond-meta-messaging-provider.workflow.json")


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


def test_inbound_channel_gateway_prefers_core_owned_delivery_with_safe_legacy_cutover():
    payload = json.loads(CHANNEL_INBOUND_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}

    webhook = nodes["Xvond Channel Inbound"]
    assert webhook["parameters"]["path"] == "xvond-channel-inbound"
    validate = nodes["Validate Normalized Message"]["parameters"]["jsCode"]

    assert "N8N_SHARED_SECRET" in validate
    assert "external_contact_id" in validate
    assert "external_message_id" in validate
    assert "XVOND_INTERNAL_CHANNEL_URL" in str(nodes["Run Xvond Employee"]["parameters"])
    assert "Core Owns Delivery?" in nodes
    gate = str(nodes["Core Owns Delivery?"]["parameters"])
    assert "delivery.delivery_id" in gate

    # During deployment the workflow is synced before the new Core container.
    # Old Core has no delivery object, so the legacy provider path remains as a
    # temporary compatibility fallback. New Core returns durable delivery state,
    # causing the workflow to return immediately without a second provider send.
    assert "Prepare Channel Reply" in nodes
    assert "Send Channel Reply" in nodes
    assert "Confirm Channel Delivery" in nodes
    true_branch = payload["connections"]["Core Owns Delivery?"]["main"][0]
    false_branch = payload["connections"]["Core Owns Delivery?"]["main"][1]
    assert true_branch[0]["node"] == "Return Channel Result"
    assert false_branch[0]["node"] == "Prepare Channel Reply"


def test_telegram_provider_normalizes_updates_and_returns_provider_message_identity():
    payload = json.loads(TELEGRAM_PROVIDER_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}

    assert nodes["Telegram Inbound"]["parameters"]["path"] == "xvond-telegram-inbound"
    assert nodes["Telegram Provider"]["parameters"]["path"] == "xvond-telegram-provider"

    normalize = nodes["Normalize Telegram"]["parameters"]["jsCode"]
    assert "x-telegram-bot-api-secret-token" in normalize.lower()
    assert "update_id" in normalize
    assert "external_contact_id" in normalize
    assert "external_message_id" in normalize
    assert "channel_type:'telegram'" in normalize
    assert "XVOND_TELEGRAM_ROUTES_JSON" in normalize
    assert "bot_token" not in normalize

    outbound = nodes["Validate Telegram Send"]["parameters"]["jsCode"]
    assert "channel.send" in outbound
    assert "provider_secret" in outbound
    assert "route_key" in outbound
    assert "route.bot_token" in outbound
    emitted = outbound.split("return [{json:{", 1)[-1]
    assert "bot_token:" not in emitted

    send = str(nodes["Telegram sendMessage"]["parameters"])
    assert "api.telegram.org" in send
    assert "sendMessage" in send
    assert "XVOND_TELEGRAM_ROUTES_JSON" in send

    result = nodes["Normalize Telegram Send Result"]["parameters"]["jsCode"]
    assert "provider_message_id" in result
    assert "message_id" in result


def test_telegram_provider_route_credentials_are_not_written_into_workflow_execution_payload():
    payload = json.loads(TELEGRAM_PROVIDER_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}
    outbound = nodes["Validate Telegram Send"]["parameters"]["jsCode"]

    # Secrets may be read from the workflow-only registry for validation/use,
    # but they must never be copied into the emitted item passed between nodes.
    emitted = outbound.split("return [{json:{", 1)[-1]
    assert "bot_token:" not in emitted
    assert "provider_secret:" not in emitted
    assert "route_key:routeKey" in emitted

    send_url = nodes["Telegram sendMessage"]["parameters"]["url"]
    assert "XVOND_TELEGRAM_ROUTES_JSON" in send_url
    assert ".bot_token" in send_url


def test_meta_messaging_provider_verifies_raw_body_signature_and_normalizes_messages():
    payload = json.loads(META_PROVIDER_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}

    assert nodes["Meta Verify"]["parameters"]["path"] == "xvond-meta-messaging"
    assert nodes["Meta Messaging Inbound"]["parameters"]["path"] == "xvond-meta-messaging"
    assert nodes["Meta Messaging Inbound"]["parameters"]["options"]["rawBody"] is True
    assert nodes["Meta Messaging Provider"]["parameters"]["path"] == "xvond-meta-messaging-provider"

    code = nodes["Validate and Normalize Meta"]["parameters"]["jsCode"]
    assert "require('crypto')" in code
    assert "x-hub-signature-256" in code.lower()
    assert "createHmac('sha256'" in code
    assert "timingSafeEqual" in code
    assert "XVOND_META_MESSAGING_ROUTES_JSON" in code
    assert "message?.mid" in code
    assert "event?.sender?.id" in code
    assert "message.is_echo === true" in code
    assert "external_contact_id" in code
    assert "external_message_id" in code
    assert "channel_type:expectedChannel" in code

    reject = nodes["Reject Meta Inbound"]
    assert reject["parameters"]["options"]["responseCode"] == 403


def test_meta_messaging_provider_sends_instagram_and_messenger_without_secret_propagation():
    payload = json.loads(META_PROVIDER_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}

    outbound = nodes["Validate Meta Send"]["parameters"]["jsCode"]
    assert "channel.send" in outbound
    assert "route.access_token" in outbound
    assert "route.provider_secret" in outbound
    emitted = outbound.split("return [{json:{", 1)[-1]
    assert "access_token:" not in emitted
    assert "app_secret:" not in emitted
    assert "provider_secret:" not in emitted
    assert "route_key:routeKey" in emitted

    send = str(nodes["Send Meta Message"]["parameters"])
    assert "graph.instagram.com" in send
    assert "graph.facebook.com" in send
    assert "XVOND_META_MESSAGING_ROUTES_JSON" in send
    assert "Authorization" in send
    assert "Bearer" in send
    assert "messaging_type" in send
    assert "RESPONSE" in send

    normalized = nodes["Normalize Meta Send Result"]["parameters"]["jsCode"]
    assert "message_id" in normalized
    assert "provider_message_id" in normalized


def test_meta_messaging_verification_uses_dedicated_verify_token():
    payload = json.loads(META_PROVIDER_PATH.read_text(encoding="utf-8"))
    nodes = {node["name"]: node for node in payload["nodes"]}
    code = nodes["Validate Meta Verification"]["parameters"]["jsCode"]

    assert "hub.verify_token" in code
    assert "hub.challenge" in code
    assert "XVOND_META_MESSAGING_VERIFY_TOKEN" in code
    assert nodes["Reject Meta Verification"]["parameters"]["options"]["responseCode"] == 403
