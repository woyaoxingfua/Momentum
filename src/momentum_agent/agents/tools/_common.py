import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...storage import TaskStore


def _to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _task_brief(t) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "status": t.status.value,
        "priority": t.priority.value,
        "due_at": t.due_at.isoformat() if t.due_at else None,
        "estimated_minutes": t.estimated_minutes,
        "parent_task_id": t.parent_task_id,
        "tags": t.tags,
    }


def _read_preferences(store: 'TaskStore', user_id: str) -> dict[str, object]:
    memory = store.get_all_memory(user_id=user_id)
    prefs: dict[str, object] = {}
    if "daily_capacity_minutes" in memory:
        try:
            prefs["daily_capacity_minutes"] = int(memory["daily_capacity_minutes"])
        except ValueError:
            pass
    if "working_hours_start" in memory:
        prefs["working_hours_start"] = memory["working_hours_start"]
    if "working_hours_end" in memory:
        prefs["working_hours_end"] = memory["working_hours_end"]
    return prefs
