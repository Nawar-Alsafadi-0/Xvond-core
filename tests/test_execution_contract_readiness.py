from backend.app.modules.ai_agent.self_service_policy import evaluate_readiness
from backend.app.modules.automation.execution_graph import graph_contract_errors


def test_execution_graph_contract_accepts_valid_pipeline():
    graph = {
        "version": 1,
        "trigger": {"type": "manual"},
        "nodes": [
            {
                "id": "draft",
                "type": "ai",
                "depends_on": [],
                "params": {"prompt": "Create the final report."},
            },
            {
                "id": "send",
                "type": "action",
                "depends_on": ["draft"],
                "params": {
                    "action_type": "send_report",
                    "arguments": {"body": "$nodes.draft.ai_response"},
                },
            },
        ],
    }

    assert graph_contract_errors(graph, graph_agent_id=1) == []


def test_execution_graph_contract_rejects_missing_runtime_parameters():
    graph = {
        "version": 1,
        "nodes": [
            {"id": "draft", "type": "ai", "params": {}},
            {"id": "fetch", "type": "http_get_json", "params": {}},
            {"id": "act", "type": "action", "params": {}},
            {"id": "notify", "type": "notify", "params": {}},
        ],
    }

    errors = graph_contract_errors(graph, graph_agent_id=1)

    assert "draft: ai node requires prompt or label" in errors
    assert "fetch: http_get_json node requires url" in errors
    assert "act: action node requires action_type" in errors
    assert "notify: notify node requires message or label" in errors


def test_execution_graph_contract_rejects_future_node_reference():
    graph = {
        "version": 1,
        "nodes": [
            {
                "id": "send",
                "type": "action",
                "params": {
                    "action_type": "send_report",
                    "arguments": {"body": "$nodes.draft.ai_response"},
                },
            },
            {
                "id": "draft",
                "type": "ai",
                "params": {"prompt": "Draft report."},
            },
        ],
    }

    errors = graph_contract_errors(graph, graph_agent_id=1)
    assert any("send: references unavailable node output(s): draft" == item for item in errors)


def test_execution_graph_contract_validates_nested_foreach_graph():
    graph = {
        "version": 1,
        "nodes": [
            {
                "id": "each",
                "type": "foreach",
                "params": {
                    "items": "$input.items",
                    "graph": {
                        "version": 1,
                        "nodes": [
                            {
                                "id": "notify",
                                "type": "notify",
                                "params": {},
                            }
                        ],
                    },
                },
            }
        ],
    }

    errors = graph_contract_errors(graph, graph_agent_id=1)
    assert "each/notify: notify node requires message or label" in errors


def test_readiness_blocks_nonready_execution_graph_trigger():
    spec = {
        "requirements": [],
        "delivery": {
            "provisioning_version": 1,
            "graph_trigger": {
                "status": "setup_required",
                "workflow_id": None,
                "trigger_type": "manual",
            },
        },
    }

    state = evaluate_readiness(
        subscribed=True,
        channel_limit=None,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )

    assert state["ready"] is False
    assert "Execution graph: trigger setup required" in state["blockers"]


def test_readiness_accepts_ready_execution_graph_trigger():
    spec = {
        "requirements": [],
        "delivery": {
            "provisioning_version": 1,
            "graph_trigger": {
                "status": "ready",
                "workflow_id": 10,
                "trigger_type": "manual",
            },
        },
    }

    state = evaluate_readiness(
        subscribed=True,
        channel_limit=None,
        requested_channels=[],
        enabled_channels=[],
        compiled_spec=spec,
        provisioned=True,
    )

    assert state["ready"] is True
    assert state["blockers"] == []

def test_execution_graph_contract_accepts_bounded_repeat_and_rejects_unbounded_repeat():
    valid = {
        "version": 1,
        "nodes": [{
            "id": "pages",
            "type": "repeat",
            "params": {
                "max_iterations": 5,
                "initial": {"next": 0},
                "until": {"path": "graph_last.next", "operator": "eq", "value": None},
                "graph": {
                    "version": 1,
                    "nodes": [{
                        "id": "fetch",
                        "type": "action",
                        "params": {
                            "agent_id": 1,
                            "action_type": "list_records",
                            "arguments": {"cursor": "$previous.next"},
                        },
                    }],
                },
            },
        }],
    }
    assert graph_contract_errors(valid, graph_agent_id=1) == []

    invalid = {
        "version": 1,
        "nodes": [{
            "id": "pages",
            "type": "repeat",
            "params": {
                "max_iterations": 21,
                "until": {"path": "graph_last.next", "operator": "eq", "value": None},
                "graph": valid["nodes"][0]["params"]["graph"],
            },
        }],
    }
    errors = graph_contract_errors(invalid, graph_agent_id=1)
    assert "pages: repeat max_iterations must be between 1 and 20" in errors
