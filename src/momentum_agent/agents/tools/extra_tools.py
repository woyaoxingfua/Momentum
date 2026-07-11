"""标签 / 笔记 / 上下文 / 每日回顾工具。

从 agent_app._build_agent 中抽离，供 Agent 与 MCP Server 复用。
"""
from typing import TYPE_CHECKING

from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def create_extra_tools(store: "TaskStore", user_id: str):
    from ._common import _to_json, _read_preferences

    @function_tool
    def get_all_tags() -> str:
        """获取所有标签"""
        return _to_json(store.get_all_tags(user_id=user_id))

    @function_tool
    def get_tasks_by_tag(tag: str) -> str:
        """获取指定标签的任务

        Args:
            tag: 标签名
        """
        tasks = store.get_tasks_by_tag(tag, user_id=user_id)
        return _to_json([
            {"id": t.id, "title": t.title, "status": t.status.value, "priority": t.priority.value}
            for t in tasks
        ])

    @function_tool
    def add_tags_to_task(task_id: int, tags: list[str]) -> str:
        """为任务添加标签

        Args:
            task_id: 任务ID
            tags: 要添加的标签列表
        """
        task = store._get_task(task_id)
        if not task or (task.user_id and task.user_id != user_id):
            return f"任务 #{task_id} 不存在"
        all_tags = list(set((task.tags or []) + tags))
        updated = store.update_task(task_id, tags=all_tags, user_id=user_id)
        return f"已更新任务 #{updated.id} 的标签" if updated else "更新失败"

    @function_tool
    def save_note(content: str) -> str:
        """保存笔记/偏好

        Args:
            content: 笔记内容
        """
        from datetime import datetime as _dt

        key = f"agent_note_{int(_dt.now().timestamp())}"
        store.set_memory(key, content, user_id=user_id)
        return "已保存笔记"

    @function_tool
    def get_my_notes() -> str:
        """获取所有笔记"""
        all_mem = store.get_all_memory(user_id=user_id)
        return _to_json({k: v for k, v in all_mem.items() if k.startswith("agent_note_")})

    @function_tool
    def get_user_context() -> str:
        """获取用户上下文（精力、可用时间等）"""
        from ...context import build_user_context

        prefs = _read_preferences(store, user_id=user_id)
        ctx = build_user_context(store.list_tasks(None, user_id=user_id), **prefs)
        return _to_json({
            "now": ctx.now.isoformat(),
            "energy": ctx.energy,
            "available_minutes_today": ctx.available_minutes_today,
        })

    @function_tool
    def get_daily_review() -> str:
        """获取每日回顾"""
        from ...context import build_user_context, daily_review as _review

        prefs = _read_preferences(store, user_id=user_id)
        ctx = build_user_context(store.list_tasks(None, user_id=user_id), **prefs)
        return _review(store.list_tasks(None, user_id=user_id), ctx)

    return [
        get_all_tags,
        get_tasks_by_tag,
        add_tags_to_task,
        save_note,
        get_my_notes,
        get_user_context,
        get_daily_review,
    ]
