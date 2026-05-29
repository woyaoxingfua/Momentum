"""
Web 处理器 - Web Handlers
提供各类 HTTP 请求处理器
"""
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .legacy_server import MomentumHandler


def task_to_json(task) -> dict:
    """将任务对象转换为 JSON 格式"""
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
        "tags": task.tags,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def handle_get_tasks(self: 'MomentumHandler', parsed) -> None:
    """GET /api/tasks - 获取任务列表"""
    from urllib.parse import parse_qs
    from ..models import TaskStatus
    
    query = parse_qs(parsed.query)
    status_param = query.get("status", [None])[0]
    
    if status_param and status_param in TaskStatus._value2member_map_:
        status = TaskStatus(status_param)
        tasks = self.store.list_tasks(status, user_id=self.user_id)
    else:
        tasks = self.store.list_tasks(status=None, user_id=self.user_id)
    
    self.send_json({"tasks": [task_to_json(t) for t in tasks]})


def handle_get_task(self: 'MomentumHandler', parsed) -> None:
    """GET /api/tasks/{id} - 获取单个任务"""
    from urllib.parse import urlparse
    
    path_parts = parsed.path.split("/")
    try:
        task_id = int(path_parts[3])
    except (IndexError, ValueError):
        self.send_json({"error": "Invalid task ID"}, HTTPStatus.BAD_REQUEST)
        return
    
    task = self.store._get_task(task_id)
    if not task:
        self.send_json({"error": "Task not found"}, HTTPStatus.NOT_FOUND)
        return
    
    if task.user_id != self.user_id:
        self.send_json({"error": "Task does not belong to you"}, HTTPStatus.FORBIDDEN)
        return
    
    self.send_json({"task": task_to_json(task)})


def handle_create_task(self: 'MomentumHandler') -> None:
    """POST /api/tasks - 创建任务"""
    from ..models import Priority
    from ..parser import parse_task_text
    from ..planner import create_plan_from_text
    
    payload = self.read_json()
    message = str(payload.get("message", "")).strip()
    
    if payload.get("plan_mode"):
        result = create_plan_from_text(self.store, message, user_id=self.user_id)
        self.send_json({"message": result})
        return
    
    if not message:
        self.send_json({"error": "消息不能为空"}, HTTPStatus.BAD_REQUEST)
        return
    
    parsed = parse_task_text(message)
    priority_str = payload.get("priority", "medium")
    chosen_priority = Priority(priority_str) if priority_str in Priority._value2member_map_ else parsed.priority
    
    task = self.store.create_task(
        parsed.title,
        due_at=parsed.due_at,
        priority=chosen_priority,
        estimated_minutes=parsed.estimated_minutes,
        notes=parsed.notes,
        tags=payload.get("tags"),
        recurrence=payload.get("recurrence"),
        user_id=self.user_id,
    )
    
    due_info = f"，截止 {task.due_at.strftime('%Y-%m-%d')}" if task.due_at else ""
    tags_info = f"，标签：{', '.join(task.tags)}" if task.tags else ""
    self.send_json({"message": f"已创建任务 #{task.id}：{task.title}{due_info}{tags_info}"})


def handle_update_task(self: 'MomentumHandler', parsed) -> None:
    """PUT /api/tasks/{id} - 更新任务"""
    from urllib.parse import urlparse
    from datetime import datetime
    
    path_parts = parsed.path.split("/")
    try:
        task_id = int(path_parts[3])
    except (IndexError, ValueError):
        self.send_json({"error": "Invalid task ID"}, HTTPStatus.BAD_REQUEST)
        return
    
    task = self.store._get_task(task_id)
    if not task or task.user_id != self.user_id:
        self.send_json({"error": "Task not found or not owned by you"}, HTTPStatus.NOT_FOUND)
        return
    
    payload = self.read_json()
    due_at = None
    if payload.get("due_at"):
        try:
            due_at = datetime.fromisoformat(payload["due_at"])
        except ValueError:
            self.send_json({"error": "Invalid due_at format"}, HTTPStatus.BAD_REQUEST)
            return
    
    updated_task = self.store.update_task(
        task_id,
        title=payload.get("title"),
        due_at=due_at,
        priority=payload.get("priority"),
        estimated_minutes=payload.get("estimated_minutes"),
        notes=payload.get("notes"),
        tags=payload.get("tags"),
        user_id=self.user_id,
    )
    
    if not updated_task:
        self.send_json({"error": "Failed to update task"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return
    
    self.send_json({"task": task_to_json(updated_task), "message": "Task updated successfully"})


def handle_delete_task(self: 'MomentumHandler', parsed) -> None:
    """DELETE /api/tasks/{id} - 删除任务"""
    path_parts = parsed.path.split("/")
    try:
        task_id = int(path_parts[3])
    except (IndexError, ValueError):
        self.send_json({"error": "Invalid task ID"}, HTTPStatus.BAD_REQUEST)
        return
    
    task = self.store._get_task(task_id)
    if not task or task.user_id != self.user_id:
        self.send_json({"error": "Task not found or not owned by you"}, HTTPStatus.NOT_FOUND)
        return
    
    self.store.update_status(task_id, task.status.DROPPED if hasattr(task.status, 'DROPPED') else self.store.list_tasks().__class__.__dict__.get('DROPPED', 'dropped'), user_id=self.user_id)
    self.send_json({"message": f"Task #{task_id} deleted successfully"})


def handle_search_tasks(self: 'MomentumHandler', parsed) -> None:
    """GET /api/search - 搜索任务"""
    from urllib.parse import parse_qs
    
    query = parse_qs(parsed.query)
    q = query.get("q", [""])[0]
    
    if not q:
        self.send_json({"error": "Search query is required"}, HTTPStatus.BAD_REQUEST)
        return
    
    tasks = self.store.search_tasks(q, user_id=self.user_id)
    self.send_json({"tasks": [task_to_json(t) for t in tasks], "count": len(tasks)})


def handle_export_data(self: 'MomentumHandler') -> None:
    """GET /api/export - 导出数据"""
    data = self.store.export_user_data(user_id=self.user_id)
    self.send_json(data)


def handle_import_data(self: 'MomentumHandler') -> None:
    """POST /api/import - 导入数据"""
    payload = self.read_json()
    count = self.store.import_user_data(payload, user_id=self.user_id)
    self.send_json({"message": f"Successfully imported {count} tasks"})


def handle_get_heartbeat_config(self: 'MomentumHandler') -> None:
    """GET /api/heartbeat/config - 获取心跳配置"""
    config = self.store.get_heartbeat_config(user_id=self.user_id)
    self.send_json(config)


def handle_set_heartbeat_config(self: 'MomentumHandler') -> None:
    """POST /api/heartbeat/config - 设置心跳配置"""
    payload = self.read_json()
    config = self.store.set_heartbeat_config(
        enabled=payload.get("enabled"),
        start_hour=payload.get("start_hour"),
        end_hour=payload.get("end_hour"),
        interval_hours=payload.get("interval_hours"),
        user_id=self.user_id,
    )
    self.send_json({"message": "Heartbeat config updated", "config": config})


def handle_get_heartbeat_suggestion(self: 'MomentumHandler') -> None:
    """GET /api/heartbeat/suggestion - 获取心跳建议"""
    from ..context import heartbeat_suggestion, build_user_context
    
    tasks = self.store.list_tasks(status=None, user_id=self.user_id)
    ctx = build_user_context(tasks)
    suggestion = heartbeat_suggestion(tasks, ctx)
    should_trigger = self.store.should_trigger_heartbeat(user_id=self.user_id)
    self.store.update_last_heartbeat(user_id=self.user_id)
    self.send_json({
        "suggestion": suggestion,
        "should_trigger": should_trigger,
        "config": self.store.get_heartbeat_config(user_id=self.user_id)
    })
