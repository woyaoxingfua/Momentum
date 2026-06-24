"""
任务管理工具 - Task Management Tools
提供任务创建、查询、编辑等工具函数
"""
import json
from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore
    from ...parser import parse_task_text


def _to_json(obj) -> str:
    """将对象转换为 JSON 字符串，确保工具输出为文本格式"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def create_task_tools(store: 'TaskStore', user_id: str):
    """创建任务相关的工具函数"""
    
    @function_tool
    def create_task(
        title: str,
        due_at: str | None = None,
        priority: str = "medium",
        notes: str | None = None,
        tags: list[str] | None = None,
        recurrence: str | None = None
    ) -> str:
        """创建任务
        
        Args:
            title: 任务标题
            due_at: 截止日期（ISO格式或自然语言）
            priority: 优先级（low/medium/high）
            notes: 备注信息
            tags: 标签列表
            recurrence: 重复规则（daily/weekly/monthly）
        """
        from ...models import Priority
        from ...parser import parse_task_text
        
        parsed = parse_task_text(f"{due_at or ''} {title}")
        chosen_priority = Priority(priority) if priority in Priority._value2member_map_ else parsed.priority
        
        task = store.create_task(
            title,
            due_at=parsed.due_at,
            priority=chosen_priority,
            estimated_minutes=parsed.estimated_minutes,
            notes=notes,
            tags=tags,
            recurrence=recurrence,
            user_id=user_id,
        )
        
        due_info = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
        rec_info = {"daily": "（每天重复）", "weekly": "（每周重复）", "monthly": "（每月重复）"}.get(task.recurrence or "", "")
        tags_info = f"，标签：{', '.join(task.tags)}" if task.tags else ""
        
        return f"已创建任务 #{task.id}：{task.title}{due_info}{rec_info}{tags_info}"
    
    @function_tool
    def list_tasks(status: str = "todo") -> str:
        """列出任务
        
        Args:
            status: 状态（todo/doing/done/dropped/all）
        """
        from ...models import TaskStatus
        
        if status == "all":
            tasks = store.list_tasks(status=None, user_id=user_id)
        else:
            chosen = TaskStatus(status) if status in TaskStatus._value2member_map_ else TaskStatus.TODO
            tasks = store.list_tasks(chosen, user_id=user_id)
        
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None,
                "estimated_minutes": t.estimated_minutes,
                "parent_task_id": t.parent_task_id,
                "recurrence": t.recurrence,
                "tags": t.tags
            }
            for t in tasks
        ])
    
    @function_tool
    def get_task(task_id: int) -> str:
        """获取任务详情
        
        Args:
            task_id: 任务ID
        """
        task = store._get_task(task_id)
        if not task or (task.user_id and task.user_id != user_id):
            return _to_json(None)
        
        return _to_json({
            "id": task.id,
            "title": task.title,
            "status": task.status.value,
            "priority": task.priority.value,
            "due_at": task.due_at.isoformat() if task.due_at else None,
            "estimated_minutes": task.estimated_minutes,
            "notes": task.notes,
            "parent_task_id": task.parent_task_id,
            "recurrence": task.recurrence,
            "tags": task.tags,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        })
    
    @function_tool
    def edit_task(
        task_id: int,
        title: str | None = None,
        due_at: str | None = None,
        priority: str | None = None,
        estimated_minutes: int | None = None,
        notes: str | None = None,
        tags: list[str] | None = None
    ) -> str:
        """编辑任务
        
        Args:
            task_id: 任务ID
            title: 新标题
            due_at: 新截止日期
            priority: 新优先级
            estimated_minutes: 新的预计时间
            notes: 新备注
            tags: 新标签
        """
        from ...models import Priority
        from datetime import datetime
        
        parsed_due = datetime.fromisoformat(due_at) if due_at else None
        parsed_pri = Priority(priority) if priority and priority in Priority._value2member_map_ else None
        
        task = store.update_task(
            task_id,
            title=title,
            due_at=parsed_due,
            priority=parsed_pri,
            estimated_minutes=estimated_minutes,
            notes=notes,
            tags=tags,
            user_id=user_id
        )
        
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        
        due = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
        tags_info = f"，标签：{', '.join(task.tags)}" if task.tags else ""
        
        return f"已更新任务 #{task.id}：{task.title}{due}{tags_info}"
    
    @function_tool
    def complete_task(task_id: int) -> str:
        """完成任务
        
        Args:
            task_id: 任务ID
        """
        task = store._get_task(task_id)
        if not task:
            return f"task #{task_id} not found"
        if task.user_id != user_id:
            return f"task #{task_id} does not belong to you"
        
        next_task = store.complete_recurring_task(task_id)
        if not next_task:
            return f"任务 #{task_id} 不存在或不属于你"
        
        if next_task.recurrence:
            return f"已完成 #{task_id}：{next_task.title}，已自动创建下一期任务 #{next_task.id}"
        
        return f"已完成 #{task_id}：{next_task.title}"
    
    @function_tool
    def start_task(task_id: int) -> str:
        """开始任务
        
        Args:
            task_id: 任务ID
        """
        task = store.start_task(task_id, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        return f"已开始 #{task.id}：{task.title}"
    
    @function_tool
    def drop_task(task_id: int) -> str:
        """放弃任务
        
        Args:
            task_id: 任务ID
        """
        task = store.drop_task(task_id, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        return f"已放弃 #{task.id}：{task.title}"
    
    @function_tool
    def reopen_task(task_id: int) -> str:
        """重新打开任务
        
        Args:
            task_id: 任务ID
        """
        task = store.reopen_task(task_id, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        return f"已重新打开任务 #{task.id}：{task.title}"
    
    @function_tool
    def postpone_task(task_id: int, days: int = 3) -> str:
        """推迟任务
        
        Args:
            task_id: 任务ID
            days: 推迟天数
        """
        task = store.postpone_task(task_id, days, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        new_due = task.due_at.strftime("%Y-%m-%d") if task.due_at else "无截止"
        return f"已推迟 #{task.id}：{task.title} → {new_due}"
    
    @function_tool
    def search_tasks(query: str) -> str:
        """搜索任务
        
        Args:
            query: 搜索关键词
        """
        return _to_json([
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None
            }
            for t in store.search_tasks(query, user_id=user_id)
        ])
    
    @function_tool
    def get_overview() -> str:
        """获取任务总览"""
        from ...models import TaskStatus
        from datetime import datetime, timedelta
        
        all_tasks = store.list_tasks(status=None, user_id=user_id)
        now = datetime.now().astimezone()
        counts = {"todo": 0, "doing": 0, "done": 0, "dropped": 0}
        overdue = 0
        due_soon = 0
        
        for t in all_tasks:
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
            if t.status in (TaskStatus.TODO, TaskStatus.DOING) and t.due_at:
                if t.due_at < now:
                    overdue += 1
                elif t.due_at < now + timedelta(days=2):
                    due_soon += 1
        
        return _to_json({
            "total": len(all_tasks),
            "by_status": counts,
            "overdue": overdue,
            "due_within_48h": due_soon,
            "top_3_todo": [
                {
                    "id": t.id,
                    "title": t.title,
                    "priority": t.priority.value,
                    "due_at": t.due_at.isoformat() if t.due_at else None
                }
                for t in all_tasks if t.status == TaskStatus.TODO
            ][:3],
        })
    
    return [
        create_task,
        list_tasks,
        get_task,
        edit_task,
        complete_task,
        start_task,
        drop_task,
        reopen_task,
        postpone_task,
        search_tasks,
        get_overview,
    ]
