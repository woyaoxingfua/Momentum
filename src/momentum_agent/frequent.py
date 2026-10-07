"""历史常完成任务 — 从「真实完成事件」聚合出可以「再来一个」的标题。

口径见 docs/PRODUCT_REQUESTS_20261007.md：
  - 数据源是真实完成事件（task_events.event_type=status_changed 且 payload=done），
    不是当前 done 状态列表；重开后再次完成会产生新的事件。
  - 只统计最近 N 天（默认 180 天）。
  - 标题按确定性标准化后「完全相同」分组，不做模糊匹配。
  - 同一标题至少出现在 2 个不同任务 ID 上才展示。
  - 排除周期任务（recurrence 非空）。
  - 按最近一次真实完成时间倒序；计数按不同任务 ID，同一 ID 重复完成不重复加权。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

DEFAULT_HISTORY_DAYS = 180
MIN_DISTINCT_TASKS = 2
MAX_FREQUENT_TASKS = 50


def normalize_title(title: str | None) -> str:
    """确定性标题标准化：NFKC + 空白折叠 + 大小写归一，幂等。"""
    if title is None:
        return ""
    text = unicodedata.normalize("NFKC", str(title))
    text = "".join(" " if ch.isspace() else ch for ch in text)
    text = " ".join(text.split())
    return text.casefold()


def history_since(days: int = DEFAULT_HISTORY_DAYS, *, now: datetime | None = None) -> datetime:
    """完成历史的起始时间（带时区）。"""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - timedelta(days=max(1, int(days)))


def _coerce_tags(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, Iterable):
        items = [str(part).strip() for part in value]
    else:
        return None
    cleaned = [item for item in items if item]
    return cleaned or None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


@dataclass
class FrequentTask:
    key: str
    title: str
    completion_count: int
    last_completed_at: datetime
    priority: str = "medium"
    estimated_minutes: int | None = None
    tags: list[str] | None = None
    task_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "completion_count": self.completion_count,
            "last_completed_at": self.last_completed_at.isoformat(),
            "priority": self.priority,
            "estimated_minutes": self.estimated_minutes,
            "tags": list(self.tags or []),
        }


def aggregate_frequent_tasks(
    rows: Iterable[dict[str, Any]] | None,
    *,
    min_distinct_tasks: int = MIN_DISTINCT_TASKS,
    limit: int = MAX_FREQUENT_TASKS,
) -> list[FrequentTask]:
    """把完成历史行聚合为可展示的「常完成任务」。"""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = normalize_title(row.get("title"))
        if not key:
            continue
        try:
            task_id = int(row.get("task_id"))
        except (TypeError, ValueError):
            continue
        completed_at = _as_datetime(row.get("completed_at"))
        if completed_at is None:
            continue
        bucket = groups.setdefault(key, {"ids": set(), "latest": None, "source": None, "title": ""})
        bucket["ids"].add(task_id)
        if bucket["latest"] is None or completed_at > bucket["latest"]:
            bucket["latest"] = completed_at
            bucket["source"] = row
            bucket["title"] = str(row.get("title") or "").strip()

    results: list[FrequentTask] = []
    for key, bucket in groups.items():
        if len(bucket["ids"]) < max(1, int(min_distinct_tasks)):
            continue
        source = bucket["source"] or {}
        priority = str(source.get("priority") or "medium")
        estimated = source.get("estimated_minutes")
        try:
            estimated = int(estimated) if estimated is not None else None
        except (TypeError, ValueError):
            estimated = None
        results.append(
            FrequentTask(
                key=key,
                title=bucket["title"] or key,
                completion_count=len(bucket["ids"]),
                last_completed_at=bucket["latest"],
                priority=priority,
                estimated_minutes=estimated,
                tags=_coerce_tags(source.get("tags")),
                task_ids=sorted(bucket["ids"]),
            )
        )
    results.sort(key=lambda item: (-item.last_completed_at.timestamp(), item.key))
    return results[: max(1, int(limit))]


def find_frequent_task(
    rows: Iterable[dict[str, Any]] | None,
    key: str,
    *,
    min_distinct_tasks: int = MIN_DISTINCT_TASKS,
) -> FrequentTask | None:
    """按 key 找回一条历史常完成任务（用于「再来一个」）。"""
    target = normalize_title(key)
    if not target:
        return None
    for item in aggregate_frequent_tasks(rows, min_distinct_tasks=min_distinct_tasks, limit=MAX_FREQUENT_TASKS):
        if item.key == target:
            return item
    return None


__all__ = [
    "DEFAULT_HISTORY_DAYS",
    "MIN_DISTINCT_TASKS",
    "MAX_FREQUENT_TASKS",
    "FrequentTask",
    "normalize_title",
    "history_since",
    "aggregate_frequent_tasks",
    "find_frequent_task",
]
