from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .logger import get_logger
from .models import Priority

log = get_logger("parser")


@dataclass(frozen=True)
class ParsedTask:
    title: str
    due_at: datetime | None
    priority: Priority
    estimated_minutes: int | None
    notes: str | None = None
    recurrence: str | None = None


def parse_task_text(text: str, *, now: datetime | None = None) -> ParsedTask:
    current = now or datetime.now().astimezone()
    normalized = text.strip()
    due_at = infer_due_at(normalized, current)
    priority = infer_priority(normalized)
    estimated = infer_estimated_minutes(normalized)
    recurrence = infer_recurrence(normalized)
    title = cleanup_title(normalized)
    result = ParsedTask(title=title, due_at=due_at, priority=priority, estimated_minutes=estimated, recurrence=recurrence)
    log.debug("parsed: title=%r due=%s priority=%s est=%s recurrence=%s", title, due_at, priority.value, estimated, recurrence)
    return result


def infer_recurrence(text: str) -> str | None:
    if "每天" in text:
        return "daily"
    if "每周" in text:
        return "weekly"
    if "每月" in text:
        return "monthly"
    return None


def infer_due_at(text: str, now: datetime) -> datetime | None:
    iso_match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if iso_match:
        year = int(iso_match.group(1))
        month = int(iso_match.group(2))
        day = int(iso_match.group(3))
        return apply_time_hint(text, now.replace(year=year, month=month, day=day, hour=18, minute=0, second=0, microsecond=0))

    if "今天" in text:
        return apply_time_hint(text, now.replace(hour=18, minute=0, second=0, microsecond=0))
    if "明天" in text:
        return apply_time_hint(text, (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0))
    if "后天" in text:
        return apply_time_hint(text, (now + timedelta(days=2)).replace(hour=18, minute=0, second=0, microsecond=0))
    if "下周" in text:
        days_until_next_monday = 7 - now.weekday()
        return apply_time_hint(
            text,
            (now + timedelta(days=days_until_next_monday)).replace(hour=18, minute=0, second=0, microsecond=0),
        )

    match = re.search(r"(\d{1,2})[月/-](\d{1,2})[日号]?", text)
    if match:
        month = int(match.group(1))
        day = int(match.group(2))
        year = now.year
        try:
            candidate = now.replace(year=year, month=month, day=day, hour=18, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        if candidate < now:
            candidate = candidate.replace(year=year + 1)
        return apply_time_hint(text, candidate)

    return None


def apply_time_hint(text: str, candidate: datetime) -> datetime:
    clock_match = re.search(r"(\d{1,2})(?:点|:)(\d{1,2})?", text)
    if clock_match:
        hour = int(clock_match.group(1))
        minute = int(clock_match.group(2) or 0)
        if "下午" in text or "晚上" in text:
            hour = hour + 12 if hour < 12 else hour
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return candidate.replace(hour=hour, minute=minute)

    if "上午" in text:
        return candidate.replace(hour=10, minute=0)
    if "中午" in text:
        return candidate.replace(hour=12, minute=0)
    if "下午" in text:
        return candidate.replace(hour=15, minute=0)
    if "晚上" in text or "今晚" in text:
        return candidate.replace(hour=20, minute=0)
    return candidate


def infer_priority(text: str) -> Priority:
    high_markers = ("紧急", "重要", "必须", "马上", "尽快")
    low_markers = ("有空", "不急", "随便")
    if any(marker in text for marker in high_markers):
        return Priority.HIGH
    if any(marker in text for marker in low_markers):
        return Priority.LOW
    return Priority.MEDIUM


def infer_estimated_minutes(text: str) -> int | None:
    minute_match = re.search(r"(\d{1,3})\s*分钟", text)
    if minute_match:
        return int(minute_match.group(1))

    hour_match = re.search(r"(\d{1,2})\s*小时", text)
    if hour_match:
        return int(hour_match.group(1)) * 60

    if any(marker in text for marker in ("整理", "准备", "研究", "写")):
        return 45
    return None


def cleanup_title(text: str) -> str:
    title = text.strip()
    title = re.sub(r"^(帮我|记一下|提醒我|我想|需要)", "", title).strip()
    title = re.sub(r"^我.+?要", "", title).strip()
    title = re.sub(r"^(安排|规划|计划|拆分)", "", title).strip()
    title = re.sub(r"(每天|每周|每月|今天|明天|后天|下周|上午|中午|下午|晚上|今晚|尽快|马上|有空|不急)", "", title).strip()
    title = re.sub(r"\d{1,2}(?:点|:)\d{0,2}", "", title).strip()
    return title or text.strip()
