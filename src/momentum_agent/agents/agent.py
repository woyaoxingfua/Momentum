"""兼容入口：create_agent_tools。

历史上这个文件内联了一批工具，与 tools/extra_tools.py 重复，而且其中的 get_daily_review
从错误的模块导入 local_review（真被调用会 ImportError）。现在统一委托给 tools 下的各工厂，
保证「工具只有一处实现」，也顺带恢复了此前只存在于这里的批量工具。
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage import TaskStore

DEFAULT_USER_ID = "default"


def create_agent_tools(store: "TaskStore", *, user_id: str = DEFAULT_USER_ID):
    """返回全部工具（统一来自 tools/*，含洞察/天气这类在主 Agent 上由专家 Agent 承担的部分）。

    主 Agent 只挂载其中一个子集，专家工具通过 handoff 提供；这里保留「全量」语义，
    仅供外部按需取用，不再自带第二套实现。
    """
    from .tools import (
        create_task_tools,
        create_subtask_tools,
        create_relation_tools,
        create_weather_tools,
        create_heartbeat_tools,
        create_insight_tools,
        create_focus_tools,
        create_extra_tools,
    )

    tools = []
    for factory in (
        create_task_tools,
        create_subtask_tools,
        create_relation_tools,
        create_weather_tools,
        create_heartbeat_tools,
        create_insight_tools,
        create_focus_tools,
        create_extra_tools,
    ):
        tools.extend(factory(store, user_id))
    return tools
