from __future__ import annotations

from calendar import monthrange
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


def _parse_once_at(value, *, default_timezone: str | None = None) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ScheduleConfigError("One-time schedule requires at")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleConfigError("One-time schedule at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        zone = _timezone(default_timezone)
        parsed = parsed.replace(tzinfo=zone)
    return _utc(parsed)


def normalize_schedule_config(value: dict | None, *, default_timezone: str | None = None) -> dict:
    if not isinstance(value, dict):
        raise ScheduleConfigError("Schedule configuration must be an object")

    kind = str(value.get("kind") or "").strip().lower()
    if kind not in {"interval", "once", "daily", "weekly", "monthly"}:
        raise ScheduleConfigError(
            "Schedule kind must be interval, once, daily, weekly, or monthly"
        )

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

    if kind == "once":
        at = _parse_once_at(value.get("at"), default_timezone=default_timezone)
        return {
            "kind": "once",
            "at": at.isoformat().replace("+00:00", "Z"),
        }

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

    if kind == "monthly":
        try:
            day_of_month = int(value.get("day_of_month"))
        except (TypeError, ValueError) as exc:
            raise ScheduleConfigError("Monthly schedule requires day_of_month") from exc
        if not 1 <= day_of_month <= 31:
            raise ScheduleConfigError("Monthly day_of_month must be between 1 and 31")
        result["day_of_month"] = day_of_month

    return result


def _monthly_candidate(
    *,
    year: int,
    month: int,
    day_of_month: int,
    hour: int,
    minute: int,
    zone: ZoneInfo,
) -> datetime:
    # Clamp 29-31 to the last valid day for shorter months. This keeps a generic
    # "run monthly on day N" schedule reliable without silently skipping months.
    valid_day = min(day_of_month, monthrange(year, month)[1])
    return datetime(
        year,
        month,
        valid_day,
        hour,
        minute,
        tzinfo=zone,
    )


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

    if normalized["kind"] == "once":
        target = _utc(
            datetime.fromisoformat(str(normalized["at"]).replace("Z", "+00:00"))
        )
        if target < created_utc or now_utc < target:
            return None
        return target

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

    if normalized["kind"] == "monthly":
        candidate = _monthly_candidate(
            year=local_now.year,
            month=local_now.month,
            day_of_month=normalized["day_of_month"],
            hour=normalized["hour"],
            minute=normalized["minute"],
            zone=zone,
        )
        if candidate > local_now:
            if local_now.month == 1:
                year, month = local_now.year - 1, 12
            else:
                year, month = local_now.year, local_now.month - 1
            candidate = _monthly_candidate(
                year=year,
                month=month,
                day_of_month=normalized["day_of_month"],
                hour=normalized["hour"],
                minute=normalized["minute"],
                zone=zone,
            )
        candidate_utc = candidate.astimezone(UTC)
        if candidate_utc < created_utc:
            return None
        return candidate_utc

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
