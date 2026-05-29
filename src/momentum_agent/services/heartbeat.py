"""
心跳服务模块 - Heartbeat Service Module
提供主动建议和定时提醒功能
"""
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage import TaskStore


class HeartbeatService:
    """心跳服务类 - 提供定期主动建议"""
    
    def __init__(self, store: 'TaskStore'):
        self.store = store
    
    def get_config(self, user_id: str = 'default') -> dict:
        """获取心跳配置
        
        Args:
            user_id: 用户ID
            
        Returns:
            配置字典
        """
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
            "last_heartbeat_at": None
        }
    
    def set_config(
        self,
        enabled: bool | None = None,
        start_hour: int | None = None,
        end_hour: int | None = None,
        interval_hours: int | None = None,
        user_id: str = 'default'
    ) -> dict:
        """更新心跳配置
        
        Args:
            enabled: 是否启用
            start_hour: 开始时间（小时）
            end_hour: 结束时间（小时）
            interval_hours: 间隔时间（小时）
            user_id: 用户ID
            
        Returns:
            更新后的配置
        """
        config = self.get_config(user_id)
        
        if enabled is not None:
            config["enabled"] = enabled
        if start_hour is not None:
            config["start_hour"] = max(0, min(23, start_hour))
        if end_hour is not None:
            config["end_hour"] = max(0, min(23, end_hour))
        if interval_hours is not None:
            config["interval_hours"] = max(1, min(24, interval_hours))
        
        self.store.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config
    
    def update_last_heartbeat(self, user_id: str = 'default') -> dict:
        """更新最后一次心跳时间
        
        Args:
            user_id: 用户ID
            
        Returns:
            更新后的配置
        """
        config = self.get_config(user_id)
        config["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        self.store.set_memory("heartbeat_config", json.dumps(config), user_id=user_id)
        return config
    
    def should_trigger(self, user_id: str = 'default') -> bool:
        """检查是否应该触发心跳
        
        Args:
            user_id: 用户ID
            
        Returns:
            是否应该触发
        """
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
        
        return heartbeat_suggestion(tasks, context)
