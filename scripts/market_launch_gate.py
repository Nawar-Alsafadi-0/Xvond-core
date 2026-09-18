from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from backend.app.core.config.settings import settings
from backend.app.core.database.connection import SessionLocal
from backend.app.models.company import Company
from backend.app.modules.ai_agent.models import AIAgent
from backend.app.modules.billing.payment_gateway import payment_gateway
from backend.app.modules.billing.service_models import (
    ServiceCheckout,
    ServicePaymentEvent,
    ServiceSubscription,
)
from backend.app.modules.channels.acceptance import customer_roundtrip_verified
from backend.app.modules.channels.catalog import (
    CHANNEL_SETUP_INTERNAL,
    canonical_channel_type,
    get_channel_capability,
    validate_channel_config,
)
from backend.app.modules.channels.models import AgentChannel
from scripts.production_acceptance import check_release


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_channels(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for raw in str(value or "").split(","):
            key = canonical_channel_type(raw)
            if key and key not in result:
                result.append(key)
    return result


def _channel_gate(
    db,
    *,
    company_id: int,
    agent_id: int,
    required_channels: list[str],
) -> dict:
    rows = (
        db.query(AgentChannel)
        .filter(
            AgentChannel.company_id == company_id,
            AgentChannel.agent_id == agent_id,
        )
        .all()
    )
    by_type = {canonical_channel_type(row.channel_type): row for row in rows}
    checks: dict[str, dict] = {}

    for channel_type in required_channels:
        capability = get_channel_capability(channel_type)
        if capability is None:
            checks[channel_type] = {
                "ok": False,
                "reason": "channel_not_registered",
            }
            continue

        if capability.get("setup_mode") == CHANNEL_SETUP_INTERNAL:
            checks[channel_type] = {
                "ok": True,
                "packaged_provider": True,
                "reason": "built_in",
            }
            continue

        packaged = capability.get("packaged_provider") is True
        row = by_type.get(channel_type)
        if row is None:
            checks[channel_type] = {
                "ok": False,
                "packaged_provider": packaged,
                "reason": "channel_not_configured",
            }
            continue

        try:
            validate_channel_config(channel_type, row.config or {})
            configured = True
            config_issue = None
        except ValueError as exc:
            configured = False
            config_issue = str(exc)

        roundtrip = customer_roundtrip_verified(row)
        checks[channel_type] = {
            "ok": bool(
                packaged
                and configured
                and row.enabled
                and roundtrip
            ),
            "packaged_provider": packaged,
            "configured": configured,
            "enabled": bool(row.enabled),
            "roundtrip_verified": roundtrip,
            "roundtrip_source": row.customer_roundtrip_source,
            "issue": config_issue,
            "reason": (
                None
                if packaged and configured and row.enabled and roundtrip
                else "provider_not_packaged"
                if not packaged
                else "configuration_incomplete"
                if not configured
                else "channel_not_enabled"
                if not row.enabled
                else "real_customer_roundtrip_missing"
            ),
        }

    return {
        "ok": all(item.get("ok") is True for item in checks.values()),
        "required": required_channels,
        "channels": checks,
    }


def _billing_gate(
    db,
    *,
    company_id: int,
    require_online_billing: bool,
    require_payment_evidence: bool,
) -> dict:
    now = _utcnow_naive()
    subscription = (
        db.query(ServiceSubscription)
        .filter(
            ServiceSubscription.company_id == company_id,
            ServiceSubscription.service_code == "ai_agents",
        )
        .first()
    )
    active_subscription = bool(
        subscription is not None
        and subscription.status == "active"
        and subscription.current_period_start <= now < subscription.current_period_end
    )

    gateway = payment_gateway()
    online_configured = bool(gateway is not None and gateway.configured())

    checkouts = []
    completed_transaction = False
    payment_event = False
    if subscription is not None:
        checkouts = (
            db.query(ServiceCheckout)
            .filter(
                ServiceCheckout.company_id == company_id,
                ServiceCheckout.service_subscription_id == subscription.id,
            )
            .order_by(ServiceCheckout.id.desc())
            .all()
        )
        completed_transaction = any(
            str(item.status or "").lower() in {"completed", "active"}
            and bool(str(item.provider_transaction_id or "").strip())
            for item in checkouts
        )

    if completed_transaction:
        checkout_ids = {
            int(item.id)
            for item in checkouts
            if str(item.status or "").lower() in {"completed", "active"}
        }
        payment_event = (
            db.query(ServicePaymentEvent)
            .filter(
                ServicePaymentEvent.company_id == company_id,
                ServicePaymentEvent.service_checkout_id.in_(checkout_ids),
                ServicePaymentEvent.provider == "paddle",
                ServicePaymentEvent.event_type == "transaction.completed",
            )
            .count()
            > 0
        )

    blockers = []
    if not active_subscription:
        blockers.append("active_ai_employee_subscription_required")
    if require_online_billing and not online_configured:
        blockers.append("online_billing_not_configured")
    if require_online_billing and settings.BILLING_PROVIDER == "paddle":
        if settings.PADDLE_ENVIRONMENT != "live":
            blockers.append("paddle_environment_not_live")
        if not str(settings.PADDLE_CHECKOUT_URL or "").startswith("https://"):
            blockers.append("paddle_checkout_url_not_https")
    if require_payment_evidence and not completed_transaction:
        blockers.append("completed_checkout_evidence_missing")
    if require_payment_evidence and not payment_event:
        blockers.append("signed_payment_webhook_evidence_missing")

    return {
        "ok": not blockers,
        "active_subscription": active_subscription,
        "online_billing_required": require_online_billing,
        "online_billing_configured": online_configured,
        "payment_provider": settings.BILLING_PROVIDER or None,
        "payment_evidence_required": require_payment_evidence,
        "completed_checkout_evidence": completed_transaction,
        "signed_payment_webhook_evidence": payment_event,
        "blockers": blockers,
    }


def market_launch_gate(
    *,
    company_id: int,
    agent_id: int,
    launch_mode: str,
    required_channels: list[str],
    require_online_billing: bool,
    require_payment_evidence: bool,
    live_ai: bool = True,
) -> dict:
    base = check_release(
        company_id=company_id,
        agent_id=agent_id,
        live_ai=live_ai,
        require_live=True,
    )

    db = SessionLocal()
    try:
        company = db.get(Company, company_id)
        agent = db.get(AIAgent, agent_id)

        identity = {
            "ok": bool(
                company is not None
                and agent is not None
                and agent.company_id == company_id
                and str(company.onboarding_source or "").strip().lower() == launch_mode
            ),
            "company_exists": company is not None,
            "agent_exists": agent is not None and getattr(agent, "company_id", None) == company_id,
            "expected_launch_mode": launch_mode,
            "actual_launch_mode": (
                str(company.onboarding_source or "").strip().lower()
                if company is not None
                else None
            ),
        }

        channels = _channel_gate(
            db,
            company_id=company_id,
            agent_id=agent_id,
            required_channels=required_channels,
        )
        billing = _billing_gate(
            db,
            company_id=company_id,
            require_online_billing=require_online_billing,
            require_payment_evidence=require_payment_evidence,
        )

        report = {
            "launchable": bool(
                base.get("overall_ok")
                and identity["ok"]
                and channels["ok"]
                and billing["ok"]
            ),
            "launch_mode": launch_mode,
            "company_id": company_id,
            "agent_id": agent_id,
            "base_acceptance": base,
            "identity": identity,
            "channels": channels,
            "billing": billing,
            "truth": {
                "code_ready_is_not_service_ready": True,
                "required_channels_need_real_roundtrip_evidence": True,
                "provider_account_permissions_are_external": True,
            },
        }
        return report
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed market launch gate for one real Xvond customer path."
    )
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--agent-id", type=int, required=True)
    parser.add_argument(
        "--launch-mode",
        choices=("managed", "self_service"),
        required=True,
    )
    parser.add_argument(
        "--require-channel",
        action="append",
        default=[],
        help="Required sold communication channel. Repeat or pass comma-separated values.",
    )
    parser.add_argument(
        "--require-online-billing",
        action="store_true",
        help="Require the configured online payment provider for this launch path.",
    )
    parser.add_argument(
        "--require-payment-evidence",
        action="store_true",
        help="Require a completed checkout and signed transaction.completed webhook evidence.",
    )
    parser.add_argument(
        "--skip-live-ai",
        action="store_true",
        help="Skip the real billable AI health request. Not recommended for final launch acceptance.",
    )
    args = parser.parse_args()

    required_channels = _parse_channels(args.require_channel)
    if not required_channels:
        parser.error("At least one --require-channel is required for market launch acceptance")
    if args.require_payment_evidence and not args.require_online_billing:
        parser.error("--require-payment-evidence requires --require-online-billing")

    report = market_launch_gate(
        company_id=args.company_id,
        agent_id=args.agent_id,
        launch_mode=args.launch_mode,
        required_channels=required_channels,
        require_online_billing=args.require_online_billing,
        require_payment_evidence=args.require_payment_evidence,
        live_ai=not args.skip_live_ai,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if report["launchable"] else 1


if __name__ == "__main__":
    sys.exit(main())
