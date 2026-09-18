from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_

from backend.app.core.database.connection import SessionLocal
from backend.app.modules.automation.models import AutomationEventOutbox


MAX_EVENT_ATTEMPTS = 5
STALE_PROCESSING_SECONDS = 300


def _utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def enqueue_automation_event(
    db,
    *,
    company_id: int,
    event_name: str,
    event_id: str,
    payload: dict | None = None,
    source_type: str,
    source_id: str | int | None = None,
) -> AutomationEventOutbox:
    clean_name = str(event_name or "").strip().lower()
    clean_event_id = str(event_id or "").strip()
    clean_source_type = str(source_type or "").strip().lower()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("Automation event name is invalid")
    if not clean_event_id or len(clean_event_id) > 200:
        raise ValueError("Automation event id is invalid")
    if not clean_source_type or len(clean_source_type) > 80:
        raise ValueError("Automation event source type is invalid")
    if not isinstance(payload or {}, dict):
        raise ValueError("Automation event payload must be an object")

    existing = (
        db.query(AutomationEventOutbox)
        .filter(
            AutomationEventOutbox.company_id == int(company_id),
            AutomationEventOutbox.event_id == clean_event_id,
        )
        .first()
    )
    if existing is not None:
        return existing

    now = _utcnow_naive()
    item = AutomationEventOutbox(
        company_id=int(company_id),
        event_name=clean_name,
        event_id=clean_event_id,
        source_type=clean_source_type,
        source_id=(str(source_id)[:120] if source_id is not None else None),
        payload=dict(payload or {}),
        status="pending",
        attempt_count=0,
        available_at=now,
        last_error=None,
        result={},
        created_at=now,
        updated_at=now,
        dispatched_at=None,
    )
    db.add(item)
    db.flush()
    return item


def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(300, 5 * (2 ** max(0, int(attempt_count) - 1)))
    return timedelta(seconds=seconds)


def dispatch_outbox_event(outbox_id: int) -> dict:
    db = SessionLocal()
    try:
        item = (
            db.query(AutomationEventOutbox)
            .filter(AutomationEventOutbox.id == int(outbox_id))
            .with_for_update()
            .first()
        )
        if item is None:
            return {"outbox_id": int(outbox_id), "status": "missing"}
        if item.status == "dispatched":
            return {
                "outbox_id": item.id,
                "status": "dispatched",
                "already_dispatched": True,
                "result": item.result or {},
            }

        now = _utcnow_naive()
        if (
            item.status == "processing"
            and item.updated_at
            and item.updated_at > now - timedelta(seconds=STALE_PROCESSING_SECONDS)
        ):
            db.rollback()
            return {"outbox_id": item.id, "status": "processing"}

        item.status = "processing"
        item.attempt_count = int(item.attempt_count or 0) + 1
        item.updated_at = now
        item.last_error = None
        company_id = item.company_id
        event_name = item.event_name
        event_id = item.event_id
        payload = dict(item.payload or {})
        attempt_count = item.attempt_count
        db.commit()
    finally:
        db.close()

    try:
        # Import lazily: event dispatch depends on the automation runtime, which
        # in turn owns action execution. Keeping this edge lazy avoids a module
        # cycle while preserving one durable event pipeline.
        from backend.app.modules.automation.event_dispatch import (
            dispatch_automation_event,
        )

        result = dispatch_automation_event(
            company_id=company_id,
            event_name=event_name,
            event_id=event_id,
            payload=payload,
        )
    except Exception as exc:
        failure_db = SessionLocal()
        try:
            item = (
                failure_db.query(AutomationEventOutbox)
                .filter(AutomationEventOutbox.id == int(outbox_id))
                .with_for_update()
                .first()
            )
            if item is None:
                raise
            item.last_error = str(exc)[:2000]
            item.updated_at = _utcnow_naive()
            if attempt_count >= MAX_EVENT_ATTEMPTS:
                item.status = "failed"
            else:
                item.status = "pending"
                item.available_at = item.updated_at + _retry_delay(attempt_count)
            failure_db.commit()
            return {
                "outbox_id": item.id,
                "status": item.status,
                "attempt_count": item.attempt_count,
                "error": item.last_error,
            }
        finally:
            failure_db.close()

    success_db = SessionLocal()
    try:
        item = (
            success_db.query(AutomationEventOutbox)
            .filter(AutomationEventOutbox.id == int(outbox_id))
            .with_for_update()
            .first()
        )
        if item is None:
            return {"outbox_id": int(outbox_id), "status": "missing_after_dispatch"}
        now = _utcnow_naive()
        item.status = "dispatched"
        item.result = dict(result or {})
        item.last_error = None
        item.updated_at = now
        item.dispatched_at = now
        success_db.commit()
        return {
            "outbox_id": item.id,
            "status": "dispatched",
            "attempt_count": item.attempt_count,
            "result": item.result,
        }
    finally:
        success_db.close()


def dispatch_pending_automation_events_once(*, limit: int = 100) -> dict:
    safe_limit = max(1, min(int(limit), 500))
    now = _utcnow_naive()
    stale_before = now - timedelta(seconds=STALE_PROCESSING_SECONDS)

    db = SessionLocal()
    try:
        ids = [
            row[0]
            for row in (
                db.query(AutomationEventOutbox.id)
                .filter(
                    AutomationEventOutbox.attempt_count < MAX_EVENT_ATTEMPTS,
                    or_(
                        (
                            (AutomationEventOutbox.status == "pending")
                            & (AutomationEventOutbox.available_at <= now)
                        ),
                        (
                            (AutomationEventOutbox.status == "processing")
                            & (AutomationEventOutbox.updated_at <= stale_before)
                        ),
                    ),
                )
                .order_by(AutomationEventOutbox.id.asc())
                .limit(safe_limit)
                .all()
            )
        ]
    finally:
        db.close()

    results = [dispatch_outbox_event(outbox_id) for outbox_id in ids]
    return {
        "checked": len(ids),
        "dispatched": sum(1 for item in results if item.get("status") == "dispatched"),
        "pending": sum(1 for item in results if item.get("status") == "pending"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "results": results,
    }
