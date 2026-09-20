from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.main import app  # noqa: F401 - register model metadata
from backend.app.core.database.base import Base
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.models import AutomationWorkflow
from backend.app.modules.tools.models import AgentToolAssignment
from scripts.generalization_acceptance import (
    dry_run_provisioning,
    evaluate_compiled_spec,
    evaluate_provisioned_spec,
)


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


def test_provisioning_gate_accepts_real_contract_and_truthful_external_blocker():
    spec = {
        "requirements": [
            {"key": "internal_report", "status": "xvond_managed"},
            {"key": "future_messenger", "status": "connection_required"},
        ],
        "execution_routines": [
            {
                "id": "daily_report",
                "graph": {
                    "nodes": [
                        {
                            "id": "store",
                            "type": "action",
                            "params": {"action_type": "internal_report"},
                        }
                    ]
                },
            },
            {
                "id": "reply",
                "graph": {
                    "nodes": [
                        {
                            "id": "send",
                            "type": "action",
                            "params": {"action_type": "future_messenger"},
                        }
                    ]
                },
            },
        ],
    }
    delivery = {
        "provisioning_version": 1,
        "unsupported": [],
        "action_plan": {
            "internal_report": {
                "status": "contract_provisioned",
                "execution_status": "ready",
            }
        },
        "graph_triggers": [
            {"routine_id": "daily_report", "status": "ready", "workflow_id": 11},
            {"routine_id": "reply", "status": "setup_required", "workflow_id": None},
        ],
    }

    result = evaluate_provisioned_spec(spec, delivery)

    assert result["ok"] is True
    assert result["action_contract_count"] == 1
    assert result["setup_blockers"] == [
        {"requirement_key": "future_messenger", "reason": "connection_required"}
    ]


def test_provisioning_gate_rejects_silent_action_and_unexplained_workflow_block():
    spec = {
        "requirements": [{"key": "silent_action", "status": "available"}],
        "execution_graph": {
            "nodes": [
                {
                    "id": "execute",
                    "type": "action",
                    "params": {"action_type": "silent_action"},
                }
            ]
        },
    }
    result = evaluate_provisioned_spec(
        spec,
        {
            "provisioning_version": 1,
            "unsupported": [],
            "action_plan": {},
            "graph_triggers": [
                {"routine_id": "primary", "status": "setup_required"}
            ],
        },
    )

    assert result["ok"] is False
    assert "action_not_provisioned_or_blocked:silent_action" in result["errors"]
    assert "routine_not_provisionable:primary:setup_required" in result["errors"]


def test_provisioning_gate_rejects_wrong_routine_identity_even_when_count_matches():
    result = evaluate_provisioned_spec(
        {
            "requirements": [],
            "execution_routines": [
                {
                    "id": "expected",
                    "graph": {"nodes": [{"id": "draft", "type": "ai"}]},
                }
            ],
        },
        {
            "provisioning_version": 1,
            "unsupported": [],
            "action_plan": {},
            "graph_triggers": [{"routine_id": "different", "status": "ready"}],
        },
    )

    assert result["ok"] is False
    assert "provisioned_routine_identity_mismatch" in result["errors"]


def test_dry_run_provisioning_exercises_real_builder_and_rolls_back_all_writes():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine, autoflush=False)
    try:
        db.add(
            Company(
                id=1,
                name="Generalization acceptance",
                active=True,
                onboarding_source="self_service",
            )
        )
        db.add(
            AIAgent(
                id=1,
                company_id=1,
                name="Acceptance employee",
                system_prompt="Acceptance only",
                provider="mock",
                model="mock",
                enabled=False,
            )
        )
        db.commit()

        result = dry_run_provisioning(
            db,
            agent_id=1,
            spec={
                "job_brief": "Capture a qualified lead inside Xvond.",
                "requirements": [
                    {
                        "key": "lead_management",
                        "kind": "action",
                        "status": "xvond_build",
                        "purpose": "Capture a qualified lead",
                        "fulfillment_mode": "xvond_internal",
                        "primitives": ["business_record"],
                    }
                ],
                "execution_graph": {
                    "trigger": {"type": "manual"},
                    "nodes": [
                        {
                            "id": "save_lead",
                            "type": "action",
                            "params": {
                                "action_type": "lead_management",
                                "arguments": {"interest": "qualified"},
                            },
                        }
                    ],
                },
            },
        )

        assert result["ok"] is True
        assert result["action_contract_count"] == 1
        assert result["workflow_count"] == 1
        assert db.query(AgentToolAssignment).count() == 0
        assert db.query(AutomationWorkflow).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_live_gate_uses_same_open_ended_compiler_contract_as_self_service():
    assert "COMPILER_SYSTEM_PROMPT" in SCRIPT
    assert "build_compiler_user_message" in SCRIPT
    assert "parse_compiler_response" in SCRIPT
    assert "runtime_selections" in SCRIPT
    assert "ai_engine.generate" in SCRIPT
    assert "graph_contract_errors" in SCRIPT
    assert "graph_action_types" in SCRIPT
    assert "provision_compiled_capabilities" in SCRIPT
    assert "dry_run_provisioning" in SCRIPT
    assert "DEFAULT_JOB_BRIEFS" in SCRIPT
    assert "does_not_persist_or_launch_customer_employees" in SCRIPT
    assert "provisioning_writes_are_rolled_back" in SCRIPT


def test_production_deploy_can_require_live_generalization_gate():
    deploy = (ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")
    assert 'GENERALIZATION_ACCEPTANCE="${GENERALIZATION_ACCEPTANCE:-false}"' in deploy
    assert 'python -m scripts.generalization_acceptance' in deploy
    assert 'Generalization gate requires ACCEPTANCE_COMPANY_ID and ACCEPTANCE_AGENT_ID' in deploy


def test_generalization_set_includes_an_unknown_communication_platform():
    assert "FutureMessenger" in SCRIPT
    assert "المنصة غير موجودة ضمن قنوات Xvond المعروفة" in SCRIPT
