from __future__ import annotations

import hashlib
import hmac

from backend.app.core.config.settings import settings


def automation_webhook_key(*, workflow_id: int, company_id: int) -> str:
    payload = f"automation-webhook:{int(company_id)}:{int(workflow_id)}".encode("utf-8")
    return hmac.new(
        settings.JWT_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def verify_automation_webhook_key(
    supplied: str,
    *,
    workflow_id: int,
    company_id: int,
) -> bool:
    expected = automation_webhook_key(
        workflow_id=workflow_id,
        company_id=company_id,
    )
    return hmac.compare_digest(str(supplied or "").strip(), expected)
