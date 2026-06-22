"""
Agent 构建器 - Agent Builder
提供 Agent 创建和工具注册功能
"""
from typing import TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from ...storage import TaskStore
    from ...config import ProviderConfig

DEFAULT_USER_ID = "default"


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
    )
    from agents import function_tool

    tools = []

    tools.extend(create_task_tools(store, user_id))
    tools.extend(create_subtask_tools(store, user_id))
    tools.extend(create_relation_tools(store, user_id))
    tools.extend(create_weather_tools(store, user_id))
    tools.extend(create_heartbeat_tools(store, user_id))
    tools.extend(create_insight_tools(store, user_id))

    @function_tool
    def get_daily_review() -> str:
        """获取每日回顾"""
        from ...context import local_review
        return local_review(store, user_id=user_id)

    @function_tool
    def get_user_context() -> dict:
        """获取用户上下文"""
        from ...context import build_user_context

        prefs = {
            "energy": "medium",
            "available_minutes_today": 240,
            "recent_pattern": "normal"
        }
        context = build_user_context(store.list_tasks(None, user_id=user_id), **prefs)
        return {
            "now": context.now.isoformat(),
            "energy": context.energy,
            "available_minutes_today": context.available_minutes_today,
            "recent_pattern": context.recent_pattern,
        }

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
    def get_my_notes() -> dict:
        """获取所有笔记"""
        all_mem = store.get_all_memory(user_id=user_id)
        return {k: v for k, v in all_mem.items() if k.startswith("agent_note_")}

    @function_tool
    def get_all_tags() -> list[str]:
        """获取所有标签"""
        return store.get_all_tags(user_id=user_id)

    @function_tool
    def get_tasks_by_tag(tag: str) -> list[dict]:
        """获取指定标签的任务

        Args:
            tag: 标签名称
        """
        tasks = store.get_tasks_by_tag(tag, user_id=user_id)
        return [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status.value,
                "priority": t.priority.value,
                "due_at": t.due_at.isoformat() if t.due_at else None,
                "tags": t.tags
            }
            for t in tasks
        ]

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
    def batch_complete_tasks(task_ids: list[int]) -> int:
        """批量完成任务

        Args:
            task_ids: 任务ID列表
        """
        from ...models import TaskStatus
        return store.batch_update_status(task_ids, TaskStatus.DONE, user_id=user_id)

    @function_tool
    def batch_start_tasks(task_ids: list[int]) -> int:
        """批量开始任务

        Args:
            task_ids: 任务ID列表
        """
        from ...models import TaskStatus
        return store.batch_update_status(task_ids, TaskStatus.DOING, user_id=user_id)

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


def build_agent(store: 'TaskStore', provider: 'ProviderConfig', openai_client, *, user_id: str = DEFAULT_USER_ID):
    """构建 Agent 实例

    Args:
        store: 任务存储
        provider: 模型配置
        openai_client: OpenAI 客户端
        user_id: 用户ID

    Returns:
        Agent 实例
    """
    from agents import Agent, OpenAIChatCompletionsModel
    from datetime import datetime
    from .tools import task_tools

    model = OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client)
    tools = create_agent_tools(store, user_id=user_id)
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    return Agent(
        name="Momentum",
        instructions=f"""你是 **Momentum**，不是聊天机器人，不是一个任务记录工具。你是一个**有自主判断力的战略伙伴**。

## 你的核心价值

用户用 Notion、Todoist 也能记录任务。他们选择你，是因为你能**思考**——你能看到他们的行为模式，发现他们自己没注意到的问题，给出数据驱动的建议。

## 当前时间
{now_str}

## 你的工作方式：三步循环

### 第一步：观察 + 思考（必须先做）
不要急着回复！先用工具搞清楚状况，**明确新任务要先查再做**：

**基础操作：**
- 用户明确给出要做的事 → 先用 search_tasks 查重，没有则 create_task 或 create_plan
- 用户说"hi"/"早"/开场白 → 拉 get_overview + get_insights + get_strategic_summary（可以同时调）
- 用户提到某个任务 → search_tasks 找到它
- 用户说做了某事 → 先 search_tasks 确认，再 complete_task

**关键规则：** 如果用户说了"做完了/做了一半/不想做了/推迟"，你**必须**找到对应任务并操作。不准只嘴上说"好的"然后什么都不做。

### 第二步：执行 + 分析（不只是记录）
- 确认要创建的任务 → create_task 或 create_plan（用 estimate_task_smart 智能预估时间）
- 确认完成的任务 → complete_task
- 需要开始的 → start_task
- 确认放弃的 → drop_task
- 需要推迟的 → postpone_task

### 第三步：战略反馈（这是你和 Notion 的核心区别）
不只是报告操作结果，而是给出**有洞察力的建议**：

- **行为洞察**："你最近完成率很高" / "你倾向于低估时间" / "你在上午效率最高"
- **模式发现**："你经常拖延这类任务" / "这个任务你已经推迟 3 次了"
- **风险预警**："过期任务在增加，建议集中清理" / "产出下降，注意休息"
- **智能建议**："基于你的历史，这个任务预估 45 分钟更合理"

## 沟通风格
- 中文，像一个聪明的朋友，不是一个执行命令的机器人
- 轻量 Markdown 让信息清晰
- 做了操作要报告，但更重要的是给出**为什么这样做更好**的建议
- 主动发现并指出问题，不要等用户问
- 信息不足就追问一句，别猜

## 工具优先级
1. **洞察工具**（最重要）：get_insights, get_strategic_summary, get_behavioral_profile — 这是你的核心价值
2. **任务工具**：create_task, complete_task, list_tasks, search_tasks — 基础操作
3. **规划工具**：create_plan, estimate_task_smart — 帮助用户更好地规划
4. **标签工具**：get_all_tags, get_tasks_by_tag — 组织和筛选
""",
        model=model,
        tools=tools,
    )
