"""Helpers for canonical focus-event payloads shared by storage backends."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any


def as_utc(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC; interpret legacy naive values as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def validate_focus_session(
    actual_seconds: Any,
    planned_minutes: Any,
    *,
    started_at: Any,
    ended_at: Any,
    outcome: Any,
    session_id: Any,
) -> tuple[int, datetime, datetime]:
    """Validate a persisted actual session using the HTTP finish contract."""
    if type(actual_seconds) is not int:
        raise ValueError("actual_seconds must be an integer")
    if type(planned_minutes) is not int or not 1 <= planned_minutes <= 120:
        raise ValueError("planned_minutes must be between 1 and 120")
    if actual_seconds < 0 or actual_seconds > planned_minutes * 60:
        raise ValueError("actual_seconds must be within the planned duration")
    if outcome not in ("completed", "stopped"):
        raise ValueError("outcome must be completed or stopped")
    if outcome == "completed" and actual_seconds != planned_minutes * 60:
        raise ValueError("completed sessions must equal the planned duration")
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", session_id):
        raise ValueError("session_id must be 32 hexadecimal characters")
    if not isinstance(started_at, datetime) or not isinstance(ended_at, datetime):
        raise ValueError("started_at and ended_at must be timezone-aware datetimes")
    if (
        started_at.tzinfo is None
        or started_at.utcoffset() is None
        or ended_at.tzinfo is None
        or ended_at.utcoffset() is None
    ):
        raise ValueError("started_at and ended_at must be timezone-aware datetimes")

    normalized_started = started_at.astimezone(timezone.utc)
    normalized_ended = ended_at.astimezone(timezone.utc)
    if normalized_ended < normalized_started:
        raise ValueError("ended_at must not precede started_at")
    latest_allowed = datetime.now(timezone.utc) + timedelta(minutes=5)
    if normalized_started > latest_allowed or normalized_ended > latest_allowed:
        raise ValueError("focus timestamps must not be more than five minutes in the future")
    return planned_minutes, normalized_started, normalized_ended


def focus_result(task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, JSON-ready result persisted for a focus session."""
    return {
        "task_id": int(task_id),
        "session_id": payload.get("session_id"),
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "planned_minutes": payload.get("planned_minutes"),
        "actual_seconds": payload.get("actual_seconds"),
        "outcome": payload.get("outcome"),
    }


def same_focus_payload(
    existing_task_id: int,
    existing: dict[str, Any],
    task_id: int,
    incoming: dict[str, Any],
) -> bool:
    """Compare all client-frozen fields by value rather than timestamp spelling."""
    fields = ("session_id", "planned_minutes", "actual_seconds", "outcome")
    if int(existing_task_id) != int(task_id):
        return False
    if any(existing.get(field) != incoming.get(field) for field in fields):
        return False
    for field in ("started_at", "ended_at"):
        if parse_timestamp(existing.get(field)) != parse_timestamp(incoming.get(field)):
            return False
    return True
