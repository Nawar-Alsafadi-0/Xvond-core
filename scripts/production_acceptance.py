import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, text

from backend.app.core.ai.engine import ai_engine
from backend.app.core.ai.provider_policy import runtime_selections
from backend.app.core.ai.routing_quality import set_quality_tier_cap
from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.core.error_safety import safe_error_label
from backend.app.core.n8n_gateway import n8n_gateway
from backend.app.core.readiness import company_readiness
from backend.app.modules.ai_agent.models import AIAgent, AIUsage
from backend.app.modules.automation.scheduler_health import automation_scheduler_health
from backend.app.modules.billing.limits import limits_service
from backend.app.modules.channels.catalog import N8N_CHANNEL_ADAPTER, get_channel_capability
from backend.app.modules.channels.models import AgentChannel, ManagedChannelOutboundDelivery
from backend.app.modules.channels.whatsapp_models import WhatsAppOutboundDelivery
from backend.app.modules.channels.whatsapp_queue import whatsapp_job_queue
from backend.app.modules.providers.models import AIModelRecord, AIProviderRecord
from backend.app.modules.tools.business_models import ActionRequest
from backend.app.modules.tools.models import AgentToolAssignment


UNRESOLVED_EXTERNAL = {"executing", "external_failed", "cancelling"}
UNRESOLVED_DELIVERY = {"failed", "unknown"}


def _backup_marker(name: str, *, expected: bool, stale_after: int) -> dict:
    if not expected:
        return {"ok": True, "status": "not_configured", "expected": False}
    path = Path(os.getenv("BACKUP_STATUS_DIR", "/backup-status")) / name
    now = int(time.time())
    try:
        epoch = int(path.read_text(encoding="utf-8").strip())
        if epoch <= 0 or epoch > now + 300:
            raise ValueError("invalid marker")
    except (OSError, ValueError):
        return {"ok": False, "status": "missing", "expected": True}
    age = max(0, now - epoch)
    return {
        "ok": age <= stale_after,
        "status": "healthy" if age <= stale_after else "stale",
        "expected": True,
        "last_success_at": datetime.fromtimestamp(epoch, UTC),
        "age_seconds": age,
        "stale_after_seconds": stale_after,
    }


def _backup_checks() -> dict:
    try:
        stale_after = int(os.getenv("BACKUP_STALE_AFTER_SECONDS", "129600"))
    except ValueError:
        stale_after = 129600
    stale_after = max(3600, min(stale_after, 604800))
    offsite_expected = bool(
        str(os.getenv("RESTIC_REPOSITORY") or "").strip()
        and str(os.getenv("RESTIC_PASSWORD") or "").strip()
    )
    local = _backup_marker("local_success_epoch", expected=True, stale_after=stale_after)
    offsite = _backup_marker(
        "offsite_success_epoch", expected=offsite_expected, stale_after=stale_after
    )
    return {
        "ok": bool(local["ok"] and offsite["ok"]),
        "local": local,
        "offsite": offsite,
    }


def _workflow_engine_check(db, *, company_id: int, agent_id: int) -> dict:
    """Verify the external execution plane whenever this employee depends on it."""

    assigned = (
        db.query(AgentToolAssignment)
        .filter(
            AgentToolAssignment.agent_id == agent_id,
            AgentToolAssignment.tool_name == "action_request",
            AgentToolAssignment.enabled.is_(True),
        )
        .first()
    )
    managed_channels = []
    for channel in (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == company_id,
            AgentChannel.agent_id == agent_id,
            AgentChannel.enabled.is_(True),
        )
        .all()
    ):
        capability = get_channel_capability(channel.channel_type) or {}
        if capability.get("runtime_adapter") == N8N_CHANNEL_ADAPTER:
            managed_channels.append(channel.channel_type)

    required_by = []
    if assigned is not None:
        required_by.append("business_actions")
    if managed_channels:
        required_by.append("managed_channels")

    if not required_by:
        return {
            "ok": True,
            "required": False,
            "configured": n8n_gateway.configured(),
            "status": "not_required",
            "required_by": [],
            "managed_channels": [],
        }

    if not n8n_gateway.configured():
        return {
            "ok": False,
            "required": True,
            "configured": False,
            "status": "not_configured",
            "error": "Workflow Engine is required by this AI employee but is not configured",
            "required_by": required_by,
            "managed_channels": sorted(set(managed_channels)),
        }

    try:
        result = n8n_gateway.execute(
            company_id=company_id,
            agent_id=agent_id,
            conversation_id=None,
            action="health_check",
            data={"source": "production_acceptance"},
        )
    except Exception as exc:
        return {
            "ok": False,
            "required": True,
            "configured": True,
            "status": "unreachable",
            "error": safe_error_label(exc),
            "required_by": required_by,
            "managed_channels": sorted(set(managed_channels)),
        }

    data = result.get("data") if isinstance(result, dict) else None
    workflow_status = str((data or {}).get("status") or "").strip().lower()
    healthy = bool(result.get("success")) and workflow_status == "ok"
    return {
        "ok": healthy,
        "required": True,
        "configured": True,
        "status": "healthy" if healthy else "invalid_health_response",
        "required_by": required_by,
        "managed_channels": sorted(set(managed_channels)),
    }


def check_release(
    company_id: int,
    agent_id: int | None = None,
    live_ai: bool = False,
    require_live: bool = False,
) -> dict:
    """Run a side-effect-minimized production acceptance gate.

    By default this is a *pre-live* gate: company setup must be ready, but runtime
    is allowed to remain off while lifecycle is onboarding/testing. ``require_live``
    adds post-launch runtime/customer-traffic requirements.
    """
    checks = {}
    db = SessionLocal()
    agent = None
    route = []
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}

        configured_revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        heads = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
        checks["migrations"] = {
            "ok": len(heads) == 1 and configured_revision == heads[0],
            "database_revision": configured_revision,
            "expected_head": heads[0] if len(heads) == 1 else heads,
        }

        try:
            queue_stats = whatsapp_job_queue.stats()
            redis_ok = whatsapp_job_queue.client is not None and bool(
                whatsapp_job_queue.client.ping()
            )
        except Exception as exc:
            redis_ok = False
            queue_stats = {"error": safe_error_label(exc)}
        checks["redis"] = {"ok": redis_ok}
        worker_configured = bool(queue_stats.get("configured", False))
        checks["whatsapp_worker"] = {
            "ok": bool(not worker_configured or queue_stats.get("worker_active", False)),
            "configured": worker_configured,
            "worker_active": bool(queue_stats.get("worker_active", False)),
            "queued": int(queue_stats.get("queued", 0) or 0),
            "processing": int(queue_stats.get("processing", 0) or 0),
            "retrying": int(queue_stats.get("retrying", 0) or 0),
            "dead": int(queue_stats.get("dead", 0) or 0),
            **({"error": queue_stats["error"]} if queue_stats.get("error") else {}),
        }

        try:
            scheduler_status = automation_scheduler_health.status()
            checks["automation_scheduler"] = {
                "ok": bool(
                    scheduler_status.get("configured")
                    and scheduler_status.get("active")
                ),
                **scheduler_status,
            }
        except Exception as exc:
            checks["automation_scheduler"] = {
                "ok": False,
                "configured": bool(automation_scheduler_health.configured),
                "active": False,
                "error": safe_error_label(exc),
            }

        checks["backups"] = _backup_checks()

        unresolved_external = (
            db.query(func.count(ActionRequest.id))
            .filter(
                ActionRequest.company_id == company_id,
                ActionRequest.status.in_(UNRESOLVED_EXTERNAL),
            )
            .scalar()
            or 0
        )
        unresolved_deliveries = (
            db.query(func.count(WhatsAppOutboundDelivery.id))
            .filter(
                WhatsAppOutboundDelivery.company_id == company_id,
                WhatsAppOutboundDelivery.status.in_(UNRESOLVED_DELIVERY),
            )
            .scalar()
            or 0
        )
        unresolved_managed_deliveries = (
            db.query(func.count(ManagedChannelOutboundDelivery.id))
            .filter(
                ManagedChannelOutboundDelivery.company_id == company_id,
                ManagedChannelOutboundDelivery.status.in_(UNRESOLVED_DELIVERY),
            )
            .scalar()
            or 0
        )
        recent_ai_failures = (
            db.query(func.count(AIUsage.id))
            .filter(
                AIUsage.company_id == company_id,
                AIUsage.status == "failed",
            )
            .scalar()
            or 0
        )
        checks["open_incidents"] = {
            "ok": not unresolved_external and not unresolved_deliveries and not unresolved_managed_deliveries,
            "unresolved_external_operations": int(unresolved_external),
            "unresolved_whatsapp_deliveries": int(unresolved_deliveries),
            "unresolved_managed_channel_deliveries": int(unresolved_managed_deliveries),
            "recorded_ai_failures": int(recent_ai_failures),
        }

        loaded = set(ai_engine.list_providers()) - {"mock"}
        rows = (
            db.query(AIModelRecord, AIProviderRecord)
            .join(AIProviderRecord, AIProviderRecord.name == AIModelRecord.provider_name)
            .filter(
                AIModelRecord.enabled.is_(True),
                AIProviderRecord.enabled.is_(True),
            )
            .all()
        )
        eligible = [
            {"provider": model.provider_name, "model": model.model_name}
            for model, provider in rows
            if provider.name in loaded
        ]
        checks["ai_providers"] = {
            "ok": bool(eligible),
            "runtime_loaded": sorted(loaded),
            "eligible_models": eligible,
        }

        readiness = company_readiness(db, company_id)
        lifecycle = str((readiness or {}).get("company", {}).get("lifecycle_status") or "")
        setup_ready = bool(readiness and readiness.get("setup_ready"))
        runtime_active = bool(readiness and readiness.get("company", {}).get("active"))
        customer_live = bool(readiness and readiness.get("ready_for_customer"))
        company_ok = setup_ready
        if require_live:
            company_ok = bool(
                setup_ready
                and runtime_active
                and lifecycle == "live"
                and customer_live
            )
        checks["company"] = {
            "ok": company_ok,
            "mode": "post-live" if require_live else "pre-live",
            "setup_ready": setup_ready,
            "runtime_active": runtime_active,
            "lifecycle_status": lifecycle or None,
            "ready_for_customer": customer_live,
            "readiness": readiness,
        }

        if agent_id is not None:
            agent = db.query(AIAgent).filter(
                AIAgent.id == agent_id,
                AIAgent.company_id == company_id,
            ).first()
            if agent is None:
                checks["routing"] = {"ok": False, "error": "AI employee not found"}
                checks["workflow_engine"] = {
                    "ok": False,
                    "required": False,
                    "configured": n8n_gateway.configured(),
                    "status": "agent_not_found",
                }
            else:
                checks["workflow_engine"] = _workflow_engine_check(
                    db,
                    company_id=company_id,
                    agent_id=agent.id,
                )
                try:
                    if settings.is_production:
                        _subscription, plan, quality_cap = limits_service.apply_ai_quality_limit(
                            db, company_id
                        )
                        checks["package_quality"] = {
                            "ok": True,
                            "plan_id": plan.id,
                            "plan_tier": plan.tier,
                            "max_quality_tier": quality_cap,
                        }
                    else:
                        set_quality_tier_cap(None)
                        checks["package_quality"] = {
                            "ok": True,
                            "skipped": True,
                            "reason": "Package enforcement is production-only",
                        }
                    route = runtime_selections(db, company_id, agent.provider, agent.model)
                    checks["routing"] = {
                        "ok": bool(route),
                        "route": [
                            {"provider": x.provider, "model": x.model, "reason": x.reason}
                            for x in route
                        ],
                    }
                except Exception as exc:
                    route = []
                    safe_error = safe_error_label(exc)
                    checks["routing"] = {"ok": False, "error": safe_error}
                    if "package_quality" not in checks:
                        checks["package_quality"] = {"ok": False, "error": safe_error}

        if live_ai:
            if agent_id is None:
                checks["live_ai"] = {"ok": False, "error": "--agent-id is required with --live-ai"}
            elif agent is None:
                checks["live_ai"] = {"ok": False, "error": "AI employee not found"}
            elif not route:
                checks["live_ai"] = {"ok": False, "error": "No eligible real AI route is available"}
            else:
                try:
                    selection = route[0]
                    result = ai_engine.generate(
                        provider_name=selection.provider,
                        system_prompt=(
                            "You are a production health-check endpoint. "
                            "Reply with exactly XVOND_OK and nothing else."
                        ),
                        user_message="Return XVOND_OK.",
                        model=selection.model,
                        tools=None,
                    )
                    response = (result.text or "").strip()
                    checks["live_ai"] = {
                        "ok": response == "XVOND_OK",
                        "provider": selection.provider,
                        "model": selection.model,
                        "response": response[:200],
                        "customer_runtime_used": False,
                    }
                except Exception as exc:
                    db.rollback()
                    checks["live_ai"] = {"ok": False, "error": safe_error_label(exc)}
    except Exception as exc:
        db.rollback()
        checks["database"] = {"ok": False, "error": safe_error_label(exc)}
    finally:
        set_quality_tier_cap(None)
        db.close()

    checks["environment"] = {"ok": settings.is_production, "value": settings.APP_ENV}
    checks["overall_ok"] = all(
        item.get("ok", False)
        for key, item in checks.items()
        if key != "overall_ok"
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Xvond before or after activating a real customer.")
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--agent-id", type=int)
    parser.add_argument(
        "--live-ai",
        action="store_true",
        help="Send one real billable provider health-check request without creating a customer conversation.",
    )
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="Use post-launch acceptance: require lifecycle=live, active company runtime and customer-ready traffic.",
    )
    args = parser.parse_args()
    report = check_release(
        company_id=args.company_id,
        agent_id=args.agent_id,
        live_ai=args.live_ai,
        require_live=args.require_live,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
