"""
Heartbeat Service - 心跳和主动建议服务

提供定时主动建议功能，支持多种建议类型

使用方式：
    from momentum_agent.services.heartbeat import HeartbeatService
    service = HeartbeatService(store)
    suggestion = service.generate_suggestion(tasks, context)
"""
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
import random

if TYPE_CHECKING:
    from ..storage import TaskStore


class HeartbeatService:
    """心跳服务类 - 提供定期主动建议"""
    
    def __init__(self, store: 'TaskStore'):
        self.store = store
        self._suggestion_templates = self._init_templates()
    
    def _init_templates(self) -> dict:
        """初始化建议模板"""
        return {
            "encourage": [
                "💪 继续保持，你今天做得很好！",
                "🌟 你的努力正在积累成果！",
                "🎯 专注前行，每一步都算数！",
                "⚡ 小步快跑，大目标分解做！",
                "🚀 动起来，完成比完美更重要！",
            ],
            "overdue": [
                "⚠️ 你有逾期任务需要注意",
                "📋 一些任务可能需要重新安排",
                "🔄 考虑推迟或放弃不再重要的任务",
            ],
            "break": [
                "☕ 休息一下，效率更高！",
                "🌿 短暂休息能让思维更清晰",
                "💭 站起来活动活动身体",
                "🎵 听首歌放松一下",
            ],
            "weather": [
                "天气不错，适合户外活动",
                "今天有点冷，注意保暖",
                "空气很好，可以开窗通风",
            ],
            "motivation": [
                "🎯 设定一个小目标来开始吧",
                "✨ 完成一件事就是进步",
                "📊 记录进度，保持可见性",
                "🏆 每完成一个任务都是胜利",
            ],
            "priority": [
                "🔥 高优先级任务需要先处理",
                "⏰ 时间敏感的任务要优先",
                "📌 聚焦最重要的目标",
            ],
            "routine": [
                "🌅 美好的一天从任务管理开始",
                "📝 回顾一下今天的计划",
                "✅ 检查任务清单，保持井井有条",
            ],
        }
    
    def get_config(self, user_id: str = 'default') -> dict:
        """获取心跳配置"""
        config_str = self.store.get_memory("heartbeat_config", user_id=user_id)
        if config_str:
            try:
                return json.loads(config_str)
            except json.JSONDecodeError:
                pass
        
        return {
            "enabled": False,
            "start_hour": 9,
            "end_hour": 21,
            "interval_hours": 4,
            "last_heartbeat_at": None,
            "preferred_types": ["encourage", "motivation", "routine"]
        }
    
    def set_config(
        self,
        enabled: bool | None = None,
        start_hour: int | None = None,
        end_hour: int | None = None,
        interval_hours: int | None = None,
        preferred_types: list[str] | None = None,
        user_id: str = 'default'
    ) -> dict:
        """更新心跳配置"""
        config = self.get_config(user_id)
        
        if enabled is not None:
            config["enabled"] = enabled
        if start_hour is not None:
            config["start_hour"] = max(0, min(23, start_hour))
        if end_hour is not None:
            config["end_hour"] = max(0, min(23, end_hour))
        if interval_hours is not None:
            config["interval_hours"] = max(1, min(24, interval_hours))
        if preferred_types is not None:
            config["preferred_types"] = preferred_types
        
        self.store.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config
    
    def update_last_heartbeat(self, user_id: str = 'default') -> dict:
        """更新最后一次心跳时间"""
        config = self.get_config(user_id)
        config["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        self.store.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config
    
    def should_trigger(self, user_id: str = 'default') -> bool:
        """检查是否应该触发心跳"""
        config = self.get_config(user_id)
        
        if not config["enabled"]:
            return False
        
        now = datetime.now().astimezone()
        current_hour = now.hour
        
        if current_hour < config["start_hour"] or current_hour > config["end_hour"]:
            return False
        
        if config["last_heartbeat_at"]:
            last_heartbeat = datetime.fromisoformat(config["last_heartbeat_at"])
            last_heartbeat = last_heartbeat.astimezone() if last_heartbeat.tzinfo else last_heartbeat.replace(tzinfo=timezone.utc).astimezone()
            hours_since = (now - last_heartbeat).total_seconds() / 3600
            if hours_since < config["interval_hours"]:
                return False
        
        return True
    
    def generate_suggestion(self, tasks: list, context: dict) -> str:
        """生成心跳建议
        
        Args:
            tasks: 任务列表
            context: 用户上下文
            
        Returns:
            建议文本
        """
        from ..context import heartbeat_suggestion
        
        # 首先使用原有的建议生成逻辑
        base_suggestion = heartbeat_suggestion(tasks, context)
        
        # 然后添加额外的鼓励
        config = self.get_config()
        preferred_types = config.get("preferred_types", ["encourage", "motivation"])
        
        # 随机选择一种建议类型
        suggestion_type = random.choice(preferred_types)
        extra = random.choice(self._suggestion_templates.get(suggestion_type, self._suggestion_templates["encourage"]))
        
        # 组合建议
        if base_suggestion:
            return f"{base_suggestion}\n\n{extra}"
        else:
            return extra
    
    def generate_contextual_suggestion(self, tasks: list, context: dict) -> dict:
        """生成上下文相关的建议（结构化）
        
        Args:
            tasks: 任务列表
            context: 用户上下文
            
        Returns:
            结构化的建议信息
        """
        suggestions = []
        
        # 逾期任务建议
        overdue = [t for t in tasks if self._is_overdue(t)]
        if overdue:
            suggestions.append({
                "type": "warning",
                "icon": "⚠️",
                "message": f"你有 {len(overdue)} 个逾期任务",
                "priority": "high",
                "action": "建议重新安排或放弃"
            })
        
        # 即将到期建议
        upcoming = [t for t in tasks if self._is_upcoming_24h(t)]
        if upcoming:
            suggestions.append({
                "type": "info",
                "icon": "⏰",
                "message": f"{len(upcoming)} 个任务即将到期",
                "priority": "medium",
                "action": "优先处理"
            })
        
        # 鼓励建议
        if not overdue and not upcoming:
            suggestions.append({
                "type": "success",
                "icon": "🎉",
                "message": "一切顺利！",
                "priority": "low",
                "action": random.choice(self._suggestion_templates["encourage"])
            })
        
        # 随机鼓励
        if random.random() > 0.5:
            suggestions.append({
                "type": "motivation",
                "icon": "✨",
                "message": random.choice(self._suggestion_templates["motivation"]),
                "priority": "low",
                "action": None
            })
        
        return {
            "suggestions": suggestions,
            "generated_at": datetime.now().astimezone().isoformat(),
            "task_count": len(tasks),
            "overdue_count": len(overdue),
            "upcoming_count": len(upcoming)
        }
    
    def _is_overdue(self, task) -> bool:
        """检查任务是否逾期"""
        from ..models import TaskStatus
        from datetime import datetime
        
        if task.status in (TaskStatus.TODO, TaskStatus.DOING) and task.due_at:
            return task.due_at < datetime.now().astimezone()
        return False
    
    def _is_upcoming_24h(self, task) -> bool:
        """检查任务是否在24小时内到期"""
        from ..models import TaskStatus
        from datetime import timedelta
        
        if task.status in (TaskStatus.TODO, TaskStatus.DOING) and task.due_at:
            now = datetime.now().astimezone()
            return now <= task.due_at <= now + timedelta(hours=24)
        return False
    
    def get_stats(self, user_id: str = 'default') -> dict:
        """获取心跳统计信息"""
        from ..models import TaskStatus
        
        all_tasks = self.store.list_tasks(status=None, user_id=user_id)
        now = datetime.now().astimezone()
        
        stats = {
            "total_tasks": len(all_tasks),
            "todo_tasks": 0,
            "doing_tasks": 0,
            "done_tasks": 0,
            "overdue_tasks": 0,
            "upcoming_tasks_24h": 0,
        }
        
        for task in all_tasks:
            if task.status == TaskStatus.TODO:
                stats["todo_tasks"] += 1
            elif task.status == TaskStatus.DOING:
                stats["doing_tasks"] += 1
            elif task.status == TaskStatus.DONE:
                stats["done_tasks"] += 1
            
            if self._is_overdue(task):
                stats["overdue_tasks"] += 1
            elif self._is_upcoming_24h(task):
                stats["upcoming_tasks_24h"] += 1
        
        return stats


# 便捷函数
def get_config(store: 'TaskStore', user_id: str = 'default') -> dict:
    """获取配置的便捷函数"""
    return HeartbeatService(store).get_config(user_id)


def set_config(
    store: 'TaskStore',
    enabled: bool | None = None,
    start_hour: int | None = None,
    end_hour: int | None = None,
    interval_hours: int | None = None,
    preferred_types: list[str] | None = None,
    user_id: str = 'default'
) -> dict:
    """设置配置的便捷函数"""
    return HeartbeatService(store).set_config(
        enabled=enabled,
        start_hour=start_hour,
        end_hour=end_hour,
        interval_hours=interval_hours,
        preferred_types=preferred_types,
        user_id=user_id
    )


def should_trigger(store: 'TaskStore', user_id: str = 'default') -> bool:
    """检查是否应该触发的便捷函数"""
    return HeartbeatService(store).should_trigger(user_id)


def generate_suggestion(store: 'TaskStore', tasks: list, context: dict) -> str:
    """生成建议的便捷函数"""
    return HeartbeatService(store).generate_suggestion(tasks, context)
