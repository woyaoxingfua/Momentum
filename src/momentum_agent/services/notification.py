"""
Notification Service - 通知和提醒服务

提供任务提醒、到期通知等功能

使用方式：
    from momentum_agent.services.notification import NotificationService
    service = NotificationService(store)
    service.create_reminder(task_id, "明天上午10点")
"""
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional
import json

if TYPE_CHECKING:
    from ..storage import TaskStore
    from ..models import TaskStatus


class NotificationService:
    """通知服务类 - 管理任务提醒和通知"""
    
    def __init__(self, store: 'TaskStore'):
        self.store = store
    
    def create_reminder(
        self,
        task_id: int,
        message: str,
        remind_at: datetime | None = None,
        user_id: str = 'default'
    ) -> dict:
        """创建任务提醒
        
        Args:
            task_id: 任务ID
            message: 提醒消息
            remind_at: 提醒时间（datetime对象）
            user_id: 用户ID
            
        Returns:
            创建的提醒信息
        """
        task = self.store._get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        
        reminder = {
            "task_id": task_id,
            "task_title": task.title,
            "message": message,
            "remind_at": remind_at.isoformat() if remind_at else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending"
        }
        
        key = f"reminder_{task_id}_{int(datetime.now().timestamp())}"
        self.store.set_memory(key, json.dumps(reminder), user_id=user_id)
        
        return reminder
    
    def get_due_reminders(self, user_id: str = 'default') -> list[dict]:
        """获取到期提醒
        
        Args:
            user_id: 用户ID
            
        Returns:
            到期的提醒列表
        """
        all_mem = self.store.get_all_memory(user_id=user_id)
        now = datetime.now(timezone.utc)
        due_reminders = []
        
        for key, value in all_mem.items():
            if not key.startswith("reminder_"):
                continue
            
            try:
                reminder = json.loads(value)
                if reminder.get("status") != "pending":
                    continue
                
                if reminder.get("remind_at"):
                    remind_at = datetime.fromisoformat(reminder["remind_at"])
                    if remind_at <= now:
                        due_reminders.append(reminder)
            except (json.JSONDecodeError, ValueError):
                continue
        
        return due_reminders
    
    def get_pending_reminders(self, user_id: str = 'default') -> list[dict]:
        """获取待处理的提醒
        
        Args:
            user_id: 用户ID
            
        Returns:
            待处理的提醒列表
        """
        all_mem = self.store.get_all_memory(user_id=user_id)
        pending = []
        
        for key, value in all_mem.items():
            if not key.startswith("reminder_"):
                continue
            
            try:
                reminder = json.loads(value)
                if reminder.get("status") == "pending":
                    pending.append(reminder)
            except (json.JSONDecodeError, ValueError):
                continue
        
        return sorted(pending, key=lambda x: x.get("remind_at", ""))
    
    def mark_reminder_done(self, task_id: int, user_id: str = 'default') -> bool:
        """标记提醒为已完成
        
        Args:
            task_id: 任务ID
            user_id: 用户ID
            
        Returns:
            是否成功
        """
        all_mem = self.store.get_all_memory(user_id=user_id)
        
        for key, value in all_mem.items():
            if not key.startswith("reminder_"):
                continue
            
            try:
                reminder = json.loads(value)
                if reminder.get("task_id") == task_id and reminder.get("status") == "pending":
                    reminder["status"] = "done"
                    reminder["done_at"] = datetime.now(timezone.utc).isoformat()
                    self.store.set_memory(key, json.dumps(reminder), user_id=user_id)
                    return True
            except (json.JSONDecodeError, ValueError):
                continue
        
        return False
    
    def get_overdue_tasks(self, user_id: str = 'default') -> list[dict]:
        """获取逾期任务
        
        Args:
            user_id: 用户ID
            
        Returns:
            逾期任务列表
        """
        from ..models import TaskStatus
        
        all_tasks = self.store.list_tasks(status=None, user_id=user_id)
        now = datetime.now().astimezone()
        overdue = []
        
        for task in all_tasks:
            if task.status in (TaskStatus.TODO, TaskStatus.DOING) and task.due_at:
                if task.due_at < now:
                    overdue.append({
                        "id": task.id,
                        "title": task.title,
                        "due_at": task.due_at.isoformat(),
                        "priority": task.priority.value,
                        "days_overdue": (now - task.due_at).days
                    })
        
        return sorted(overdue, key=lambda x: x["days_overdue"], reverse=True)
    
    def get_upcoming_tasks(self, hours: int = 24, user_id: str = 'default') -> list[dict]:
        """获取即将到期的任务
        
        Args:
            hours: 未来多少小时内的任务
            user_id: 用户ID
            
        Returns:
            即将到期的任务列表
        """
        from ..models import TaskStatus
        
        all_tasks = self.store.list_tasks(status=None, user_id=user_id)
        now = datetime.now().astimezone()
        deadline = now + timedelta(hours=hours)
        upcoming = []
        
        for task in all_tasks:
            if task.status in (TaskStatus.TODO, TaskStatus.DOING) and task.due_at:
                if now <= task.due_at <= deadline:
                    hours_until = (task.due_at - now).total_seconds() / 3600
                    upcoming.append({
                        "id": task.id,
                        "title": task.title,
                        "due_at": task.due_at.isoformat(),
                        "priority": task.priority.value,
                        "hours_until": round(hours_until, 1)
                    })
        
        return sorted(upcoming, key=lambda x: x["hours_until"])
    
    def create_due_notification(self, task_id: int, user_id: str = 'default') -> dict:
        """为任务创建到期提醒
        
        Args:
            task_id: 任务ID
            user_id: 用户ID
            
        Returns:
            创建的通知信息
        """
        task = self.store._get_task(task_id)
        if not task:
            return {"error": "Task not found"}
        
        if not task.due_at:
            return {"error": "Task has no due date"}
        
        if task.due_at <= datetime.now().astimezone():
            message = f"⚠️ 任务「{task.title}」已到期！"
        else:
            time_until = task.due_at - datetime.now().astimezone()
            hours = time_until.total_seconds() / 3600
            if hours < 1:
                message = f"⏰ 任务「{task.title}」即将到期！"
            elif hours < 24:
                message = f"📅 任务「{task.title}」将在{hours:.0f}小时后到期"
            else:
                days = hours / 24
                message = f"📅 任务「{task.title}」将在{days:.0f}天后到期"
        
        return self.create_reminder(task_id, message, task.due_at, user_id)
    
    def generate_summary(self, user_id: str = 'default') -> dict:
        """生成任务总结
        
        Args:
            user_id: 用户ID
            
        Returns:
            任务总结信息
        """
        from ..models import TaskStatus
        
        all_tasks = self.store.list_tasks(status=None, user_id=user_id)
        now = datetime.now().astimezone()
        
        stats = {
            "total": len(all_tasks),
            "todo": 0,
            "doing": 0,
            "done": 0,
            "overdue": 0,
            "upcoming_24h": 0,
        }
        
        for task in all_tasks:
            if task.status == TaskStatus.TODO:
                stats["todo"] += 1
            elif task.status == TaskStatus.DOING:
                stats["doing"] += 1
            elif task.status == TaskStatus.DONE:
                stats["done"] += 1
            
            if task.due_at:
                if task.due_at < now:
                    stats["overdue"] += 1
                elif (task.due_at - now).total_seconds() <= 86400:  # 24 hours
                    stats["upcoming_24h"] += 1
        
        pending_reminders = self.get_pending_reminders(user_id)
        due_reminders = self.get_due_reminders(user_id)
        
        return {
            "stats": stats,
            "pending_reminders": len(pending_reminders),
            "due_reminders": len(due_reminders),
            "generated_at": now.isoformat()
        }


# 便捷函数
def create_reminder(
    store: 'TaskStore',
    task_id: int,
    message: str,
    remind_at: datetime | None = None,
    user_id: str = 'default'
) -> dict:
    """创建提醒的便捷函数"""
    return NotificationService(store).create_reminder(task_id, message, remind_at, user_id)


def get_overdue_tasks(store: 'TaskStore', user_id: str = 'default') -> list[dict]:
    """获取逾期任务的便捷函数"""
    return NotificationService(store).get_overdue_tasks(user_id)


def generate_summary(store: 'TaskStore', user_id: str = 'default') -> dict:
    """生成总结的便捷函数"""
    return NotificationService(store).generate_summary(user_id)
