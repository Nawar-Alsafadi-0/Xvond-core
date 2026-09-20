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
                    attempt = {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        **evaluation,
                    }
                except Exception as exc:
                    attempt = {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "ok": False,
                        "errors": [f"compiler_error:{type(exc).__name__}"],
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
                "does_not_prove_external_provider_connections": True,
                "does_not_create_or_launch_customer_employees": True,
            },
        }
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live Xvond AI Employee compiler generalization gate. "
            "Compiles diverse Job Briefs without creating customer employees."
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
