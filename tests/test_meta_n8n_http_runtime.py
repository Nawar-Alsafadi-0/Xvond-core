import json
from pathlib import Path


WORKFLOW_PATH = Path("ops/n8n/xvond-meta-messaging-provider.workflow.json")
COMPOSE_PATH = Path("docker-compose.production.yml")


def _code_nodes():
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return {
        node["id"]: node.get("parameters", {}).get("jsCode", "")
        for node in workflow.get("nodes", [])
        if node.get("type") == "n8n-nodes-base.code"
    }


def test_meta_workflow_avoids_global_fetch_in_code_nodes():
    code = _code_nodes()
    for node_id in ("validate-normalize-meta", "validate-meta-send", "send-meta-message"):
        assert "await fetch(" not in code[node_id]
        assert "xvondJsonRequest" in code[node_id]


def test_meta_workflow_allows_http_builtins_for_code_nodes():
    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    assert "NODE_FUNCTION_ALLOW_BUILTIN: crypto,http,https" in compose
