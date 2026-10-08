"""心跳 - 定时提醒的配置和触发判断。

配置读写与「是否该触发」统一以 store 的方法为唯一实现（SQLite 与 MySQL 各有一套），
本模块只做薄封装：先前这里自己实现了一份等价逻辑，一旦两边改动不同步，
Agent 工具（check_in/get_system_status）与 Web 界面（/api/heartbeat/*）就会表现不一致。
stats() 没有对应的 store 方法，仍在本模块实现。
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..models import TaskStatus

if TYPE_CHECKING:
    from ..storage import TaskStore


def get_config(store: "TaskStore", user_id: str = "default") -> dict:
    return store.get_heartbeat_config(user_id=user_id)


def set_config(
    store: "TaskStore",
    *,
    enabled: bool | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
    interval_hours: int | None = None,
    user_id: str = "default",
) -> dict:
    return store.set_heartbeat_config(
        enabled=enabled,
        start_hour=start_hour,
        end_hour=end_hour,
        interval_hours=interval_hours,
        user_id=user_id,
    )


def update_last_heartbeat(store: "TaskStore", user_id: str = "default") -> dict:
    return store.update_last_heartbeat(user_id=user_id)


def should_trigger(store: "TaskStore", user_id: str = "default") -> bool:
    return store.should_trigger_heartbeat(user_id=user_id)


def stats(store: "TaskStore", user_id: str = "default") -> dict:
    """任务概览计数（本模块自有实现，store 未提供等价方法）。"""
    tasks = store.list_tasks(status=None, user_id=user_id)
    now = datetime.now().astimezone()
    counts = {"todo": 0, "doing": 0, "done": 0, "dropped": 0, "overdue": 0, "upcoming_24h": 0}
    for task in tasks:
        counts[task.status.value] = counts.get(task.status.value, 0) + 1
        if task.due_at and task.status in (TaskStatus.TODO, TaskStatus.DOING):
            if task.due_at < now:
                counts["overdue"] += 1
            elif (task.due_at - now).total_seconds() <= 86400:
                counts["upcoming_24h"] += 1
    counts["total"] = len(tasks)
    return counts

