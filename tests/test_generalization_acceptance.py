from pathlib import Path

from scripts.generalization_acceptance import evaluate_compiled_spec


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "generalization_acceptance.py").read_text(
    encoding="utf-8"
)


def test_generalization_gate_accepts_novel_generic_execution_graph():
    spec = {
        "role": "Unusual digital worker",
        "requirements": [
            {
                "key": "mystery_vendor",
                "kind": "integration",
                "status": "connection_required",
            }
        ],
        "unsupported_requirements": [],
        "execution_graph": {
            "version": 1,
            "trigger": {"type": "manual"},
            "nodes": [
                {
                    "id": "prepare",
                    "type": "ai",
                    "depends_on": [],
                    "params": {"prompt": "Prepare the vendor request."},
                },
                {
                    "id": "execute",
                    "type": "action",
                    "depends_on": ["prepare"],
                    "params": {
                        "action_type": "mystery_vendor",
                        "arguments": {"payload": "$nodes.prepare.ai_response"},
                    },
                },
            ],
        },
    }

    result = evaluate_compiled_spec(spec, agent_id=77)

    assert result["ok"] is True
    assert result["routine_count"] == 1
    assert result["node_count"] == 2
    assert result["graphs"][0]["action_types"] == ["mystery_vendor"]


def test_generalization_gate_rejects_action_without_requirement_contract():
    spec = {
        "requirements": [],
        "unsupported_requirements": [],
        "execution_graph": {
            "version": 1,
            "nodes": [
                {
                    "id": "send",
                    "type": "action",
                    "params": {"action_type": "invented_external_action"},
                }
            ],
        },
    }

    result = evaluate_compiled_spec(spec, agent_id=9)

    assert result["ok"] is False
    assert "invalid_graph:primary" in result["errors"]
    assert "action requirement(s) missing: invented_external_action" in result["graphs"][0]["errors"]


def test_generalization_gate_rejects_unsupported_or_non_executable_result():
    result = evaluate_compiled_spec(
        {
            "role": "Rejected worker",
            "requirements": [],
            "unsupported_requirements": ["unusual capability"],
        },
        agent_id=1,
    )

    assert result["ok"] is False
    assert "compiler_returned_unsupported_requirements" in result["errors"]
    assert "no_execution_graph" in result["errors"]


def test_live_gate_uses_same_open_ended_compiler_contract_as_self_service():
    assert "COMPILER_SYSTEM_PROMPT" in SCRIPT
    assert "build_compiler_user_message" in SCRIPT
    assert "parse_compiler_response" in SCRIPT
    assert "runtime_selections" in SCRIPT
    assert "ai_engine.generate" in SCRIPT
    assert "graph_contract_errors" in SCRIPT
    assert "graph_action_types" in SCRIPT
    assert "DEFAULT_JOB_BRIEFS" in SCRIPT
    assert "does_not_create_or_launch_customer_employees" in SCRIPT


def test_production_deploy_can_require_live_generalization_gate():
    deploy = (ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
    assert 'GENERALIZATION_ACCEPTANCE="${GENERALIZATION_ACCEPTANCE:-false}"' in deploy
    assert 'python -m scripts.generalization_acceptance' in deploy
    assert 'Generalization gate requires ACCEPTANCE_COMPANY_ID and ACCEPTANCE_AGENT_ID' in deploy


def test_generalization_set_includes_an_unknown_communication_platform():
    assert "FutureMessenger" in SCRIPT
    assert "المنصة غير موجودة ضمن قنوات Xvond المعروفة" in SCRIPT
