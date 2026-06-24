"""
Agent 构建器 - Agent Builder
提供 Agent 创建和工具注册功能
"""
import json
from typing import TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from ...storage import TaskStore
    from ...config import ProviderConfig

DEFAULT_USER_ID = "default"


def _to_json(obj) -> str:
    """将对象转换为 JSON 字符串，确保工具输出为文本格式"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def create_agent_tools(store: 'TaskStore', *, user_id: str = DEFAULT_USER_ID):
    """创建所有 Agent 工具

    Args:
        store: 任务存储实例
        user_id: 用户ID

    Returns:
        工具函数列表
    """
    from .tools import (
        create_task_tools,
        create_subtask_tools,
        create_relation_tools,
        create_weather_tools,
        create_heartbeat_tools,
        create_insight_tools,
        create_focus_tools,
    )
    from agents import function_tool

    tools = []

    tools.extend(create_task_tools(store, user_id))
    tools.extend(create_subtask_tools(store, user_id))
    tools.extend(create_relation_tools(store, user_id))
    tools.extend(create_weather_tools(store, user_id))
    tools.extend(create_heartbeat_tools(store, user_id))
    tools.extend(create_insight_tools(store, user_id))
    tools.extend(create_focus_tools(store, user_id))

    @function_tool
    def get_daily_review() -> str:
        """获取每日回顾"""
        from ...context import local_review
        return local_review(store, user_id=user_id)

    @function_tool
    def get_user_context() -> str:
        """获取用户上下文"""
        from ...context import build_user_context

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

        context = build_user_context(store.list_tasks(None, user_id=user_id), **prefs)
        return _to_json({
            "now": context.now.isoformat(),
            "energy": context.energy,
            "available_minutes_today": context.available_minutes_today,
            "recent_pattern": context.recent_pattern,
        })

    @function_tool
    def save_note(content: str) -> str:
        """保存笔记

        Args:
            content: 笔记内容
        """
        from datetime import datetime
        key = f"agent_note_{int(datetime.now().timestamp())}"
        store.set_memory(key, content, user_id=user_id)
        return f"note saved: {content[:80]}"

    @function_tool
    def get_my_notes() -> str:
        """获取所有笔记"""
        all_mem = store.get_all_memory(user_id=user_id)
        return _to_json({k: v for k, v in all_mem.items() if k.startswith("agent_note_")})

    @function_tool
    def get_all_tags() -> str:
        """获取所有标签"""
        return _to_json(store.get_all_tags(user_id=user_id))

    @function_tool
    def get_tasks_by_tag(tag: str) -> str:
        """获取指定标签的任务

        Args:
            tag: 标签名称
        """
        tasks = store.get_tasks_by_tag(tag, user_id=user_id)
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None,
                "tags": t.tags
            }
            for t in tasks
        ])

    @function_tool
    def add_tags_to_task(task_id: int, tags: list[str]) -> str:
        """为任务添加标签

        Args:
            task_id: 任务ID
            tags: 标签列表
        """
        task = store._get_task(task_id)
        if not task or (task.user_id and task.user_id != user_id):
            return f"任务 #{task_id} 不存在或不属于你"

        existing_tags = task.tags or []
        all_tags = list(set(existing_tags + tags))
        updated_task = store.update_task(task_id, tags=all_tags, user_id=user_id)
        if not updated_task:
            return f"更新任务 #{task_id} 失败"

        tags_info = f"，标签：{', '.join(updated_task.tags)}" if updated_task.tags else ""
        return f"已更新任务 #{updated_task.id}：{updated_task.title}{tags_info}"

    @function_tool
    def batch_complete_tasks(task_ids: list[int]) -> str:
        """批量完成任务

        Args:
            task_ids: 任务ID列表
        """
        from ...models import TaskStatus
        count = store.batch_update_status(task_ids, TaskStatus.DONE, user_id=user_id)
        return f"已完成 {count} 个任务"

    @function_tool
    def batch_start_tasks(task_ids: list[int]) -> str:
        """批量开始任务

        Args:
            task_ids: 任务ID列表
        """
        from ...models import TaskStatus
        count = store.batch_update_status(task_ids, TaskStatus.DOING, user_id=user_id)
        return f"已开始 {count} 个任务"

    tools.extend([
        get_daily_review,
        get_user_context,
        save_note,
        get_my_notes,
        get_all_tags,
        get_tasks_by_tag,
        add_tags_to_task,
        batch_complete_tasks,
        batch_start_tasks,
    ])

    return tools
