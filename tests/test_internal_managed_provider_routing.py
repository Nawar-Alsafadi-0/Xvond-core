import json
from pathlib import Path

WORKFLOW = Path("ops/n8n/xvond-actions.workflow.json")
COMPOSE = Path("docker-compose.production.yml")


def test_managed_provider_calls_use_internal_workflow_base():
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    node = next(item for item in workflow["nodes"] if item["id"] == "normalize-route-lookup")
    code = node["parameters"]["jsCode"]
    assert "XVOND_WORKFLOW_INTERNAL_PROVIDER_BASE" in code
    assert "/webhook/" in code
    assert "_dispatch:'channel_provider'" in code


def test_compose_defines_internal_workflow_provider_base():
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "XVOND_WORKFLOW_INTERNAL_PROVIDER_BASE" in compose
    assert "http://workflow-engine:5678" in compose
