from typing import TYPE_CHECKING
from agents import function_tool
from pydantic import BaseModel

if TYPE_CHECKING:
    from ...storage import TaskStore


class SubtaskInput(BaseModel):
    title: str
    due_at: str | None = None
    priority: str = "medium"
    notes: str | None = None
    tags: list[str] | None = None


def create_subtask_tools(store: 'TaskStore', user_id: str):
    from ...models import Priority
    from ...parser import parse_task_text
    from ._common import _to_json

    @function_tool
    def create_subtask(
        parent_task_id: int,
        title: str,
        due_at: str | None = None,
        priority: str = "medium",
        notes: str | None = None,
        tags: list[str] | None = None
    ) -> str:
        """创建子任务

        Args:
            parent_task_id: 父任务ID
            title: 子任务标题
            due_at: 截止日期
            priority: 优先级（low/medium/high）
            notes: 备注
            tags: 标签
        """
        parsed = parse_task_text(f"{due_at or ''} {title}")
        chosen_priority = Priority(priority) if priority in Priority._value2member_map_ else parsed.priority

        task = store.create_subtask(
            parent_task_id,
            title,
            due_at=parsed.due_at,
            priority=chosen_priority,
            estimated_minutes=parsed.estimated_minutes,
            notes=notes,
            tags=tags,
            user_id=user_id,
        )

        due_info = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
        tags_info = f"，标签：{', '.join(task.tags)}" if task.tags else ""

        return f"已创建子任务 #{task.id}（父任务 #{parent_task_id}）：{task.title}{due_info}{tags_info}"

    @function_tool
    def get_task_with_subtasks(task_id: int) -> str:
        """获取任务及其子任务

        Args:
            task_id: 父任务ID
        """
        task = store.get_task_with_subtasks(task_id, user_id=user_id)
        if not task:
            return _to_json(None)

        return _to_json({
            "id": task.id,
            "title": task.title,
            "status": task.status.value,
            "priority": task.priority.value,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "notes": task.notes,
            "tags": task.tags,
            "subtasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "due_at": t.due_at.isoformat() if t.due_at else None,
                    "tags": t.tags
                }
                for t in task.subtasks or []
            ]
        })

    @function_tool
    def bulk_create_subtasks(parent_task_id: int, subtasks: list[SubtaskInput]) -> str:
        """批量创建子任务

        Args:
            parent_task_id: 父任务ID
            subtasks: 子任务列表（包含title、due_at、priority等）
        """
        subtask_dicts = [s.model_dump() for s in subtasks]
        created = store.bulk_create_subtasks(parent_task_id, subtask_dicts, user_id=user_id)
        return f"已为任务 #{parent_task_id} 创建 {len(created)} 个子任务：{', '.join(t.title for t in created)}"

    @function_tool
    def get_subtasks(parent_task_id: int) -> str:
        """获取父任务的所有子任务

        Args:
            parent_task_id: 父任务ID
        """
        subtasks = store.get_subtasks(parent_task_id, user_id=user_id)
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None,
                "tags": t.tags
            }
            for t in subtasks
        ])

    return [
        create_subtask,
        get_task_with_subtasks,
        bulk_create_subtasks,
        get_subtasks,
    ]
