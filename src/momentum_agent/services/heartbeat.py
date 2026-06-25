"""心跳 - 定时提醒的配置和触发判断。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..models import TaskStatus

if TYPE_CHECKING:
    from ..storage import TaskStore


def get_config(store: "TaskStore", user_id: str = "default") -> dict:
    raw = store.get_memory("heartbeat_config", user_id=user_id)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {
        "enabled": False,
        "start_hour": 9,
        "end_hour": 21,
        "interval_hours": 4,
        "last_heartbeat_at": None,
    }


def set_config(
    store: "TaskStore",
    *,
    enabled: bool | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
    interval_hours: int | None = None,
    user_id: str = "default",
) -> dict:
    cfg = get_config(store, user_id)
    if enabled is not None:
        cfg["enabled"] = enabled
    if start_hour is not None:
        cfg["start_hour"] = max(0, min(23, start_hour))
    if end_hour is not None:
        cfg["end_hour"] = max(0, min(23, end_hour))
    if interval_hours is not None:
        cfg["interval_hours"] = max(1, min(24, interval_hours))
    store.set_memory("heartbeat_config", json.dumps(cfg), user_id=user_id)
    return cfg


def update_last_heartbeat(store: "TaskStore", user_id: str = "default") -> dict:
    cfg = get_config(store, user_id)
    cfg["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
    store.set_memory("heartbeat_config", json.dumps(cfg), user_id=user_id)
    return cfg


def should_trigger(store: "TaskStore", user_id: str = "default") -> bool:
    cfg = get_config(store, user_id)
    if not cfg["enabled"]:
        return False

    now = datetime.now().astimezone()
    if now.hour < cfg["start_hour"] or now.hour > cfg["end_hour"]:
        return False

    last = cfg.get("last_heartbeat_at")
    if last:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
        hours_since = (now - dt).total_seconds() / 3600
        if hours_since < cfg["interval_hours"]:
            return False

    return True


def stats(store: "TaskStore", user_id: str = "default") -> dict:
    tasks = store.list_tasks(status=None, user_id=user_id)
    now = datetime.now().astimezone()
    counts = {"todo": 0, "doing": 0, "done": 0, "dropped": 0, "overdue": 0, "upcoming_24h": 0}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
        if t.due_at and t.status in (TaskStatus.TODO, TaskStatus.DOING):
            if t.due_at < now:
                counts["overdue"] += 1
            elif (t.due_at - now).total_seconds() <= 86400:
                counts["upcoming_24h"] += 1
    counts["total"] = len(tasks)
    return counts
