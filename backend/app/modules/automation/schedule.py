from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 60 * 24 * 30
WEEKDAYS = frozenset(range(7))


class ScheduleConfigError(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timezone(name: str | None) -> ZoneInfo:
    key = str(name or "").strip()
    if not key:
        raise ScheduleConfigError("Schedule timezone is required")
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError as exc:
        raise ScheduleConfigError("Schedule timezone is invalid") from exc


def normalize_schedule_config(value: dict | None, *, default_timezone: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ScheduleConfigError("Schedule configuration must be an object")

    kind = str(value.get("kind") or "").strip().lower()
    if kind not in {"interval", "daily", "weekly"}:
        raise ScheduleConfigError("Schedule kind must be interval, daily, or weekly")

    if kind == "interval":
        try:
            every_minutes = int(value.get("every_minutes"))
        except (TypeError, ValueError) as exc:
            raise ScheduleConfigError("Interval schedule requires every_minutes") from exc
        if not MIN_INTERVAL_MINUTES <= every_minutes <= MAX_INTERVAL_MINUTES:
            raise ScheduleConfigError(
                f"Interval must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes"
            )
        result = {"kind": "interval", "every_minutes": every_minutes}
        anchor_at = value.get("anchor_at")
        if anchor_at:
            try:
                parsed = datetime.fromisoformat(str(anchor_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ScheduleConfigError("Schedule anchor_at must be ISO-8601") from exc
            result["anchor_at"] = _utc(parsed).isoformat().replace("+00:00", "Z")
        return result

    try:
        hour = int(value.get("hour"))
        minute = int(value.get("minute", 0))
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigError("Scheduled time requires hour and minute") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleConfigError("Scheduled hour/minute is invalid")

    timezone_name = str(value.get("timezone") or default_timezone or "").strip()
    _timezone(timezone_name)

    result = {
        "kind": kind,
        "hour": hour,
        "minute": minute,
        "timezone": timezone_name,
    }

    if kind == "weekly":
        weekdays = []
        for raw in value.get("weekdays") or []:
            try:
                day = int(raw)
            except (TypeError, ValueError) as exc:
                raise ScheduleConfigError("Weekly weekdays must be integers 0-6") from exc
            if day not in WEEKDAYS:
                raise ScheduleConfigError("Weekly weekdays must be integers 0-6")
            if day not in weekdays:
                weekdays.append(day)
        if not weekdays:
            raise ScheduleConfigError("Weekly schedule requires at least one weekday")
        result["weekdays"] = sorted(weekdays)

    return result


def latest_due_slot(
    schedule: dict,
    *,
    now: datetime,
    created_at: datetime,
) -> datetime | None:
    normalized = normalize_schedule_config(schedule)
    now_utc = _utc(now)
    created_utc = _utc(created_at)

    if normalized["kind"] == "interval":
        raw_anchor = normalized.get("anchor_at")
        if raw_anchor:
            anchor = _utc(datetime.fromisoformat(str(raw_anchor).replace("Z", "+00:00")))
        else:
            anchor = created_utc
        if now_utc < anchor:
            return None
        period = timedelta(minutes=int(normalized["every_minutes"]))
        steps = int((now_utc - anchor).total_seconds() // period.total_seconds())
        return anchor + (period * steps)

    zone = _timezone(normalized["timezone"])
    local_now = now_utc.astimezone(zone)

    if normalized["kind"] == "daily":
        candidate = local_now.replace(
            hour=normalized["hour"],
            minute=normalized["minute"],
            second=0,
            microsecond=0,
        )
        if candidate > local_now:
            candidate -= timedelta(days=1)
        if candidate.astimezone(UTC) < created_utc:
            return None
        return candidate.astimezone(UTC)

    weekdays = set(normalized["weekdays"])
    for offset in range(0, 8):
        local_day = local_now - timedelta(days=offset)
        if local_day.weekday() not in weekdays:
            continue
        candidate = local_day.replace(
            hour=normalized["hour"],
            minute=normalized["minute"],
            second=0,
            microsecond=0,
        )
        if candidate > local_now:
            continue
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc < created_utc:
            return None
        return candidate_utc
    return None


def schedule_slot_key(slot: datetime) -> str:
    return _utc(slot).replace(microsecond=0).isoformat().replace("+00:00", "Z")
