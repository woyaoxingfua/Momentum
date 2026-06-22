"""Web 工具函数 — 共享的辅助逻辑。"""
from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import MomentumHandler


def task_to_json(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status.value,
        "priority": task.priority.value,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "estimated_minutes": task.estimated_minutes,
        "notes": task.notes,
        "parent_task_id": task.parent_task_id,
        "recurrence": task.recurrence,
        "user_id": task.user_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "tags": task.tags,
    }


def extract_task_id(handler: MomentumHandler, path: str, suffix: str) -> int | None:
    parts = path.strip("/").split("/")
    try:
        idx = parts.index(suffix)
        return int(parts[idx - 1])
    except (IndexError, ValueError):
        handler.send_json({"error": "任务 ID 无效。"}, HTTPStatus.BAD_REQUEST)
        return None


def extract_task_id_from_path(path: str) -> int:
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part == "tasks" and i + 1 < len(parts):
            try:
                return int(parts[i + 1])
            except ValueError:
                pass
    return -1
