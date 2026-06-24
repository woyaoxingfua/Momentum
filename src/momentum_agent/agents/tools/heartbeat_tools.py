"""心跳和状态工具 - Heartbeat Tools
提供心跳检测和状态报告工具
"""
import json
from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore
    from ...services.heartbeat import HeartbeatService


def _to_json(obj) -> str:
    """将对象转换为 JSON 字符串，确保工具输出为文本格式"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def create_heartbeat_tools(store: 'TaskStore', user_id: str):
    """创建心跳和状态报告相关的工具函数"""
    from ...services.heartbeat import HeartbeatService
    
    heartbeat_service = HeartbeatService(store)
    
    @function_tool
    def get_system_status() -> str:
        """获取系统当前状态概览
        包括：待办任务数、进行中任务数、逾期任务数、今日已完成任务数
        """
        return _to_json(heartbeat_service.get_system_status(user_id))
    
    @function_tool
    def generate_suggestion(context: str | None = None) -> str:
        """生成心跳建议
        基于当前任务状态生成建议（鼓励、提醒、任务推荐）
        
        Args:
            context: 附加上下文信息
        """
        ctx = {"context": context} if context else {}
        suggestion = heartbeat_service.generate_suggestion(
            tasks=store.list_tasks(status=None, user_id=user_id),
            context=ctx,
            user_id=user_id,
        )
        return suggestion
    
    @function_tool
    def get_daily_summary() -> str:
        """获取每日任务摘要
        生成今天需要关注的任务和建议
        """
        return _to_json(heartbeat_service.get_daily_summary(user_id))
    
    @function_tool
    def check_in() -> str:
        """签到/打卡
        记录用户当前状态并返回鼓励
        """
        return _to_json(heartbeat_service.check_in(user_id))
    
    return [
        get_system_status,
        generate_suggestion,
        get_daily_summary,
        check_in,
    ]
