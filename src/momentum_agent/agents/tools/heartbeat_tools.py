"""
心跳工具 - Heartbeat Tools
提供心跳配置和主动建议功能
"""
from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def create_heartbeat_tools(store: 'TaskStore', user_id: str):
    """创建心跳相关的工具函数"""
    
    @function_tool
    def get_heartbeat_config() -> dict:
        """获取心跳配置"""
        from ...services.heartbeat import HeartbeatService
        
        service = HeartbeatService(store)
        return service.get_config(user_id)
    
    @function_tool
    def set_heartbeat_config(
        enabled: bool | None = None,
        start_hour: int | None = None,
        end_hour: int | None = None,
        interval_hours: int | None = None
    ) -> dict:
        """设置心跳配置
        
        Args:
            enabled: 是否启用
            start_hour: 开始时间（小时）
            end_hour: 结束时间（小时）
            interval_hours: 间隔（小时）
        """
        from ...services.heartbeat import HeartbeatService
        
        service = HeartbeatService(store)
        config = service.set_config(
            enabled=enabled,
            start_hour=start_hour,
            end_hour=end_hour,
            interval_hours=interval_hours,
            user_id=user_id
        )
        
        status = "已启用" if config["enabled"] else "已禁用"
        return {
            "status": status,
            "config": config
        }
    
    @function_tool
    def should_trigger_heartbeat() -> bool:
        """检查是否应该触发心跳"""
        from ...services.heartbeat import HeartbeatService
        
        service = HeartbeatService(store)
        return service.should_trigger(user_id)
    
    @function_tool
    def get_heartbeat_suggestion() -> str:
        """获取心跳建议"""
        from ...services.heartbeat import HeartbeatService
        from ...context import build_user_context
        
        tasks = store.list_tasks(status=None, user_id=user_id)
        prefs = {
            "energy": "medium",
            "available_minutes_today": 240,
            "recent_pattern": "normal"
        }
        context = build_user_context(tasks, **prefs)
        
        service = HeartbeatService(store)
        suggestion = service.generate_suggestion(tasks, context)
        service.update_last_heartbeat(user_id)
        
        return suggestion
    
    return [
        get_heartbeat_config,
        set_heartbeat_config,
        should_trigger_heartbeat,
        get_heartbeat_suggestion,
    ]
