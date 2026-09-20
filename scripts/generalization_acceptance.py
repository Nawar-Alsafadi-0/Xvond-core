from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from backend.app.core.ai.engine import ai_engine
from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.database.connection import SessionLocal
from backend.app.modules.ai_agent.employee_compiler import (
    COMPILER_SYSTEM_PROMPT,
    build_compiler_user_message,
    parse_compiler_response,
)
from backend.app.modules.ai_agent.employee_capability_builder import (
    CUSTOMER_STATUSES,
    MANAGED_STATUS,
    provision_compiled_capabilities,
)
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.automation.execution_graph import (
    graph_action_types,
    graph_contract_errors,
)


DEFAULT_JOB_BRIEFS = (
    (
        "كل 45 دقيقة افحص https://example.com/metrics، قارن القيمة الحالية بآخر قيمة "
        "محفوظة، وخزن النتيجة الجديدة ونبهني فقط إذا تغيرت بأكثر من 15 بالمئة."
    ),
    (
        "When a webhook says a dataset batch is ready, fetch every page until "
        "next_cursor is null, aggregate the record count, save the summary, and "
        "notify me when the batch is complete."
    ),
    (
        "كل صباح اقرأ صفحة عامة وأعطني ملخصًا، وبشكل مستقل إذا وصل حدث داخلي "
        "باسم content_ready أنشئ صورة مناسبة واحفظ نتيجة المهمة."
    ),
    (
        "استخدم منصة مراسلة اسمها FutureMessenger: لما توصل رسالة عبر webhook "
        "حللها ورد عليها من API المنصة. المنصة غير موجودة ضمن قنوات Xvond المعروفة."
    ),
)


def _compiled_graphs(spec: dict) -> list[tuple[str, dict]]:
    routines = spec.get("execution_routines")
    result: list[tuple[str, dict]] = []
    if isinstance(routines, list):
        for index, routine in enumerate(routines):
            if not isinstance(routine, dict):
                continue
            graph = routine.get("graph")
            if not isinstance(graph, dict):
                continue
            routine_id = str(
                routine.get("id") or routine.get("name") or f"routine_{index + 1}"
            ).strip()
            result.append((routine_id or f"routine_{index + 1}", graph))
    if result:
        return result

    graph = spec.get("execution_graph")
    if isinstance(graph, dict):
        return [("primary", graph)]
    return []


def evaluate_compiled_spec(spec: dict, *, agent_id: int) -> dict:
    """Check that an open-ended compiler result is executable without a named use-case contract."""

    errors: list[str] = []
    unsupported = [
        str(item).strip()
        for item in (spec.get("unsupported_requirements") or [])
        if str(item).strip()
    ]
    if unsupported:
        errors.append("compiler_returned_unsupported_requirements")

    requirements = [
        item for item in (spec.get("requirements") or []) if isinstance(item, dict)
    ]
    requirement_keys = {
        str(item.get("key") or "").strip()
        for item in requirements
        if str(item.get("key") or "").strip()
    }

    graphs = _compiled_graphs(spec)
    if not graphs:
        errors.append("no_execution_graph")

    graph_reports: list[dict[str, Any]] = []
    total_nodes = 0
    for routine_id, graph in graphs:
        nodes = [item for item in (graph.get("nodes") or []) if isinstance(item, dict)]
        total_nodes += len(nodes)
        contract_errors = graph_contract_errors(graph, graph_agent_id=agent_id)
        action_types = graph_action_types(graph)
        missing_action_requirements = sorted(
            action_type
            for action_type in action_types
            if action_type not in requirement_keys
        )
        if not nodes:
            contract_errors = [*contract_errors, "execution graph has no nodes"]
        if missing_action_requirements:
            contract_errors = [
                *contract_errors,
                "action requirement(s) missing: "
                + ", ".join(missing_action_requirements),
            ]
        if contract_errors:
            errors.append(f"invalid_graph:{routine_id}")

        graph_reports.append(
            {
                "routine_id": routine_id,
                "node_count": len(nodes),
                "action_types": action_types,
                "errors": contract_errors,
            }
        )

    return {
        "ok": not errors,
        "role": str(spec.get("role") or "").strip() or None,
        "requirement_count": len(requirements),
        "requirement_keys": sorted(requirement_keys),
        "routine_count": len(graphs),
        "node_count": total_nodes,
        "graphs": graph_reports,
        "errors": errors,
    }


def evaluate_provisioned_spec(spec: dict, delivery: dict) -> dict:
    """Verify that compilation becomes an executable or truthfully blocked build."""

    errors: list[str] = []
    blockers: list[dict[str, str]] = []
    requirements = {
        str(item.get("key") or "").strip(): item
        for item in (spec.get("requirements") or [])
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }
    action_plan = {
        str(key): value
        for key, value in (delivery.get("action_plan") or {}).items()
        if isinstance(value, dict)
    }
    graphs = _compiled_graphs(spec)
    graph_actions = sorted(
        {
            action_type
            for _, graph in graphs
            for action_type in graph_action_types(graph)
        }
    )

    if delivery.get("provisioning_version") != 1:
        errors.append("provisioning_version_missing")
    if delivery.get("unsupported"):
        errors.append("provisioning_returned_unsupported_requirements")

    for action_type in graph_actions:
        requirement = requirements.get(action_type) or {}
        plan = action_plan.get(action_type)
        if plan is not None and plan.get("status") == "contract_provisioned":
            execution_status = str(plan.get("execution_status") or "").strip()
            if execution_status != "ready":
                blockers.append(
                    {
                        "requirement_key": action_type,
                        "reason": execution_status or "execution_setup_required",
                    }
                )
            continue

        requirement_status = str(requirement.get("status") or "").strip()
        if requirement_status in CUSTOMER_STATUSES:
            blockers.append(
                {
                    "requirement_key": action_type,
                    "reason": requirement_status,
                }
            )
            continue
        errors.append(f"action_not_provisioned_or_blocked:{action_type}")

    graph_triggers = [
        item
        for item in (delivery.get("graph_triggers") or [])
        if isinstance(item, dict)
    ]
    if len(graph_triggers) != len(graphs):
        errors.append("provisioned_routine_count_mismatch")
    expected_routine_ids = {routine_id for routine_id, _ in graphs}
    provisioned_routine_ids = {
        str(item.get("routine_id") or "primary").strip() or "primary"
        for item in graph_triggers
    }
    if provisioned_routine_ids != expected_routine_ids:
        errors.append("provisioned_routine_identity_mismatch")

    blocker_keys = {item["requirement_key"] for item in blockers}
    workflow_reports: list[dict[str, Any]] = []
    for item in graph_triggers:
        routine_id = str(item.get("routine_id") or "primary").strip() or "primary"
        status = str(item.get("status") or "").strip()
        routine = next((graph for key, graph in graphs if key == routine_id), {})
        routine_actions = set(graph_action_types(routine))
        truthfully_blocked = bool(routine_actions & blocker_keys)
        if status != "ready" and not (status == "setup_required" and truthfully_blocked):
            errors.append(f"routine_not_provisionable:{routine_id}:{status or 'missing'}")
        workflow_reports.append(
            {
                "routine_id": routine_id,
                "status": status or None,
                "workflow_id": item.get("workflow_id"),
                "truthfully_blocked": truthfully_blocked,
            }
        )

    managed_requirements = sorted(
        key
        for key, item in requirements.items()
        if str(item.get("status") or "").strip() == MANAGED_STATUS
    )
    return {
        "ok": not errors,
        "provisioning_version": delivery.get("provisioning_version"),
        "action_contract_count": len(action_plan),
        "managed_requirements": managed_requirements,
        "setup_blockers": blockers,
        "workflow_count": len(graph_triggers),
        "workflows": workflow_reports,
        "errors": errors,
    }


def dry_run_provisioning(db, *, agent_id: int, spec: dict) -> dict:
    """Exercise real provisioning inside a savepoint and always discard writes."""

    savepoint = db.begin_nested()
    try:
        prepared, delivery = provision_compiled_capabilities(
            db,
            agent_id=agent_id,
            spec=spec,
        )
        db.flush()
        return evaluate_provisioned_spec(prepared, delivery)
    finally:
        savepoint.rollback()
        db.expire_all()


def run_generalization_gate(
    *,
    company_id: int,
    agent_id: int,
    job_briefs: list[str] | tuple[str, ...] | None = None,
) -> dict:
    briefs = [
        str(item or "").strip()
        for item in (job_briefs or DEFAULT_JOB_BRIEFS)
        if str(item or "").strip()
    ]
    if not briefs:
        raise ValueError("At least one Job Brief is required")

    db = SessionLocal()
    try:
        agent = (
            db.query(AIAgent)
            .filter(AIAgent.id == agent_id, AIAgent.company_id == company_id)
            .first()
        )
        if agent is None:
            return {
                "ok": False,
                "company_id": company_id,
                "agent_id": agent_id,
                "cases": [],
                "error": "AI employee not found",
            }

        cases: list[dict] = []
        for index, brief in enumerate(briefs, start=1):
            selections = runtime_selections(
                db,
                company_id,
                agent.provider,
                agent.model,
                message=brief,
            )
            if not selections:
                cases.append(
                    {
                        "case": index,
                        "ok": False,
                        "job_brief": brief,
                        "error": "No eligible AI provider/model is available",
                    }
                )
                continue

            attempts: list[dict] = []
            accepted: dict | None = None
            for candidate in selections:
                stage = "compiler"
                try:
                    response = ai_engine.generate(
                        provider_name=candidate.provider,
                        system_prompt=COMPILER_SYSTEM_PROMPT,
                        user_message=build_compiler_user_message(
                            job_brief=brief,
                            requested_channels=[],
                            available_connections=[],
                        ),
                        model=candidate.model,
                        tools=None,
                    )
                    spec = parse_compiler_response(response.text, job_brief=brief)
                    evaluation = evaluate_compiled_spec(spec, agent_id=agent.id)
                    stage = "provisioning"
                    provisioning = (
                        dry_run_provisioning(db, agent_id=agent.id, spec=spec)
                        if evaluation.get("ok") is True
                        else {
                            "ok": False,
                            "errors": ["compile_contract_invalid"],
                        }
                    )
                    attempt = {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        **evaluation,
                        "ok": bool(evaluation.get("ok") and provisioning.get("ok")),
                        "provisioning": provisioning,
                    }
                except Exception as exc:
                    attempt = {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "ok": False,
                        "errors": [f"{stage}_error:{type(exc).__name__}"],
                    }
                attempts.append(attempt)
                if attempt.get("ok") is True:
                    accepted = attempt
                    break

            cases.append(
                {
                    "case": index,
                    "ok": accepted is not None,
                    "job_brief": brief,
                    "accepted": accepted,
                    "attempts": attempts,
                }
            )

        return {
            "ok": bool(cases) and all(item.get("ok") is True for item in cases),
            "company_id": company_id,
            "agent_id": agent_id,
            "case_count": len(cases),
            "cases": cases,
            "truth": {
                "tests_open_ended_composition": True,
                "tests_real_provisioning_contract": True,
                "does_not_prove_external_provider_connections": True,
                "does_not_persist_or_launch_customer_employees": True,
                "provisioning_writes_are_rolled_back": True,
            },
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live Xvond AI Employee compiler generalization gate. "
            "Compiles and dry-run provisions diverse Job Briefs without persisting "
            "or launching customer employees."
        )
    )
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument(
        "--job-brief",
        action="append",
        default=[],
        help=(
            "Optional Job Brief to compile. Repeat for multiple cases. "
            "When omitted, Xvond's built-in diverse acceptance set is used."
        ),
    )
    args = parser.parse_args()

    report = run_generalization_gate(
        company_id=args.company_id,
        agent_id=args.agent_id,
        job_briefs=args.job_brief or None,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
