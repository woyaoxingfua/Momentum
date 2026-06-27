from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from .config import DEFAULT_USER_ID, ProviderConfig, get_current_user, load_provider_config
from .context import build_user_context, choose_next_action, daily_review
from .logger import get_logger
from .models import ParsedTaskOutput, PlanOutput, Priority, TaskStatus
from .parser import ParsedTask, parse_task_text
from .planner import create_task_plan
from .storage import TaskStore, create_task_store

log = get_logger("agent")

# ═══════════════════════════════════════════════════════════════════
# 对话历史管理 — 基于 to_input_list() 的多轮记忆
# ═══════════════════════════════════════════════════════════════════

_conversation_history: dict[str, list] = {}
MAX_HISTORY_ITEMS = 40  # 保留最近 40 条消息（约 20 轮对话）

# Agent 实例缓存 — 避免每次请求都重建长 system prompt 和工具定义
_agent_cache: dict[tuple, object] = {}


def _get_history(user_id: str) -> list:
    """获取用户的对话历史"""
    return list(_conversation_history.get(user_id, []))


def _save_history(user_id: str, history: list) -> None:
    """保存对话历史，截断到最近 MAX_HISTORY 条"""
    _conversation_history[user_id] = history[-MAX_HISTORY_ITEMS:]


def _clear_history(user_id: str) -> None:
    """清除用户对话历史"""
    _conversation_history.pop(user_id, None)


__all__ = [
    "create_task_from_text",
    "create_plan_from_text",
    "local_advice",
    "local_review",
    "edit_task_from_params",
    "postpone_task_cmd",
    "drop_task_cmd",
    "start_task_cmd",
    "reopen_task_cmd",
    "get_user_config_cmd",
    "set_user_config_cmd",
    "run_agent_message",
    "run_agent_message_stream",
    "provider_status",
    "clear_conversation_history",
]


def _parsed_to_message(parsed: ParsedTask, store: TaskStore, *, user_id: str = DEFAULT_USER_ID) -> str:
    task = store.create_task(
        parsed.title,
        due_at=parsed.due_at,
        priority=parsed.priority,
        estimated_minutes=parsed.estimated_minutes,
        notes=parsed.notes,
        recurrence=parsed.recurrence,
        user_id=user_id,
    )
    due = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
    recurrence_label = {"daily": "（每天重复）", "weekly": "（每周重复）", "monthly": "（每月重复）"}.get(parsed.recurrence or "", "")
    return f"已创建任务 #{task.id}：{task.title}{due}{recurrence_label}"


def create_task_from_text(store: TaskStore, text: str, *, user_id: str = DEFAULT_USER_ID, images: list[str] | None = None) -> str:
    log.info("create_task_from_text user=%r text=%r has_images=%s", user_id, text[:80] if text else "", bool(images))
    user_config = store.get_all_memory(user_id=user_id)
    provider = load_provider_config(user_config)
    vision_enabled = user_config.get("vision_enabled", "false") == "true"

    if images and provider.is_configured:
        if not vision_enabled:
            return "抱歉，您当前未启用视觉功能。请在偏好设置中开启「启用视觉功能」选项后再上传图片。"
        try:
            parsed = asyncio.run(_parse_task_with_ai(text, provider, images=images))
            return _parsed_to_message(parsed, store, user_id=user_id)
        except Exception as exc:
            log.warning("AI vision parse failed, falling back to regex: %s", exc)
            if not text:
                return "抱歉，AI 识别图片失败了。请手动输入任务内容。"
            parsed = parse_task_text(text)
    elif provider.is_configured:
        try:
            parsed = asyncio.run(_parse_task_with_ai(text, provider))
        except Exception as exc:
            log.warning("AI parse failed, falling back to regex: %s", exc)
            parsed = parse_task_text(text)
    else:
        if images:
            return "图片识别功能需要配置 AI 模型。请在偏好设置中配置 API Key。"
        parsed = parse_task_text(text)
    return _parsed_to_message(parsed, store, user_id=user_id)


def create_plan_from_text(store: TaskStore, text: str, *, user_id: str = DEFAULT_USER_ID) -> str:
    log.info("create_plan_from_text user=%r text=%r", user_id, text)
    user_config = store.get_all_memory(user_id=user_id)
    provider = load_provider_config(user_config)
    if provider.is_configured:
        try:
            return asyncio.run(_plan_task_with_ai(text, provider, store, user_id=user_id))
        except Exception as exc:
            log.warning("AI plan failed, falling back to templates: %s", exc)
    parsed = parse_task_text(text)
    parent, children = create_task_plan(store, text, user_id=user_id)
    child_lines = "；".join(f"#{task.id} {task.title}" for task in children)
    return f"已规划任务 #{parent.id}：{parent.title}。子任务：{child_lines}"


async def _plan_task_with_ai(
    text: str, provider: ProviderConfig, store: TaskStore, *, user_id: str = DEFAULT_USER_ID
) -> str:
    from agents import Agent, OpenAIChatCompletionsModel, Runner

    openai_client = build_openai_client(provider)
    agent = Agent(
        name="task_planner",
        instructions="""你是一个任务拆解专家。把用户的目标拆成 3-5 个具体、可执行的子任务。

拆解原则：
1. 每个子任务控制在 10-60 分钟内可完成
2. 子任务之间要有先后逻辑：先收集信息，再动手做，最后检查
3. 子任务标题要具体，包含动作动词（梳理/写出/查找/对比/整理/提交/发送/确认）
4. 总时间不要超过父任务的合理范围（通常 60-180 分钟）
5. 用中文输出
""",
        model=OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client),
        output_type=PlanOutput,
    )
    result = await Runner.run(agent, text)
    output = result.final_output_as(PlanOutput)

    parsed = parse_task_text(text)
    parent = store.create_task(
        output.title,
        due_at=parsed.due_at,
        priority=parsed.priority,
        estimated_minutes=sum(s.estimated_minutes for s in output.subtasks),
        notes=parsed.notes,
        recurrence=parsed.recurrence,
        user_id=user_id,
    )

    children = [
        store.create_task(
            s.title,
            due_at=parsed.due_at,
            priority=Priority.HIGH if parsed.priority == Priority.HIGH else Priority.MEDIUM,
            estimated_minutes=s.estimated_minutes,
            parent_task_id=parent.id,
            user_id=user_id,
        )
        for s in output.subtasks
    ]

    log.info("AI plan created: parent=#%d children=%d", parent.id, len(children))
    child_lines = "；".join(f"#{task.id} {task.title}" for task in children)
    return f"已规划任务 #{parent.id}：{parent.title}。子任务：{child_lines}"


async def _parse_task_with_ai(
    text: str, provider: ProviderConfig, *, images: list[str] | None = None
) -> ParsedTask:
    """Unified AI task parser — handles text-only and multimodal (text + images)."""
    from agents import Agent, OpenAIChatCompletionsModel, Runner

    openai_client = build_openai_client(provider)

    instructions = """
You are a precise task parser. Extract structured task information from the user's input.

Rules:
1. title: Remove date words (今天/明天/后天/下周), time words (上午/中午/下午/晚上), priority words (紧急/重要/尽快/必须/马上/有空/不急/随便), and conversational prefixes (帮我/记一下/提醒我/我想/需要/安排/规划/计划/拆分). Keep the core action.
2. due_at: Parse relative dates into ISO 8601. Default to 18:00 if no time specified. Return null if no deadline.
3. priority: "high" for 紧急/重要/必须/马上/尽快. "low" for 有空/不急/随便. Otherwise "medium".
4. estimated_minutes: explicit "N分钟" or "N小时" → convert to minutes. Heuristic: 整理/准备/研究/写 → 45. Return null if unclear.
5. notes: Any extra context not captured above, or null.
6. When images are provided, extract task information from them (dates, priorities, action items).
"""
    if images:
        instructions += "\nAnalyze the provided images carefully and extract task information from visual content."

    agent = Agent(
        name="Task Parser",
        instructions=instructions,
        model=OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client),
        output_type=ParsedTaskOutput,
    )

    if images:
        content = []
        if text:
            content.append({"type": "text", "text": text})
        for img_base64 in images:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}})
        result = await Runner.run(agent, [{"role": "user", "content": content}])
    else:
        result = await Runner.run(agent, text)

    output = result.final_output_as(ParsedTaskOutput)
    due_at = datetime.fromisoformat(output.due_at) if output.due_at else None
    recurrence = None
    if "每天" in text:
        recurrence = "daily"
    elif "每周" in text:
        recurrence = "weekly"
    elif "每月" in text:
        recurrence = "monthly"
    return ParsedTask(
        title=output.title,
        due_at=due_at,
        priority=Priority(output.priority),
        estimated_minutes=output.estimated_minutes,
        notes=output.notes,
        recurrence=recurrence,
    )


def local_advice(store: TaskStore, *, user_id: str = DEFAULT_USER_ID) -> str:
    tasks = store.list_tasks(TaskStatus.TODO, user_id=user_id)
    prefs = _read_preferences(store, user_id=user_id)
    context = build_user_context(tasks, **prefs)
    return choose_next_action(tasks, context)


def local_review(store: TaskStore, *, user_id: str = DEFAULT_USER_ID) -> str:
    tasks = store.list_tasks(TaskStatus.TODO, user_id=user_id)
    prefs = _read_preferences(store, user_id=user_id)
    context = build_user_context(tasks, **prefs)
    return daily_review(tasks, context)


def _read_preferences(store: TaskStore, *, user_id: str = DEFAULT_USER_ID) -> dict[str, object]:
    memory = store.get_all_memory(user_id=user_id)
    prefs: dict[str, object] = {}
    if "daily_capacity_minutes" in memory:
        try:
            prefs["daily_capacity_minutes"] = int(memory["daily_capacity_minutes"])
        except ValueError:
            pass
    if "working_hours_start" in memory:
        prefs["working_hours_start"] = memory["working_hours_start"]
    if "working_hours_end" in memory:
        prefs["working_hours_end"] = memory["working_hours_end"]
    return prefs


def edit_task_from_params(
    store: TaskStore,
    task_id: int,
    *,
    title: str | None = None,
    due_at: str | None = None,
    priority: str | None = None,
    estimated_minutes: int | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> str:
    log.info("edit_task id=%d user=%r", task_id, user_id)
    parsed_due: datetime | None = None
    if due_at is not None:
        try:
            parsed_due = datetime.fromisoformat(due_at)
        except ValueError:
            return f"日期格式无效：{due_at}。请用 ISO 格式，如 2026-06-01 或 2026-06-01T15:00。"
    parsed_priority = None
    if priority is not None:
        if priority not in Priority._value2member_map_:
            return f"无效优先级：{priority}。可选：low, medium, high。"
        parsed_priority = Priority(priority)
    task = store.update_task(
        task_id,
        title=title,
        due_at=parsed_due,
        priority=parsed_priority,
        estimated_minutes=estimated_minutes,
        notes=notes,
        tags=tags,
        user_id=user_id,
    )
    if task is None:
        return f"没有找到任务 #{task_id}。"
    due = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
    return f"已更新任务 #{task.id}：{task.title}{due}"


def postpone_task_cmd(store: TaskStore, task_id: int, days: int, *, user_id: str = DEFAULT_USER_ID) -> str:
    task = store.postpone_task(task_id, days, user_id=user_id)
    if task is None:
        return f"没有找到任务 #{task_id} 或任务不属于你。"
    due = task.due_at.strftime("%Y-%m-%d %H:%M") if task.due_at else "无截止"
    return f"任务 #{task.id}「{task.title}」已推迟至 {due}"


def drop_task_cmd(store: TaskStore, task_id: int, *, user_id: str = DEFAULT_USER_ID) -> str:
    task = store.drop_task(task_id, user_id=user_id)
    if task is None:
        return f"没有找到任务 #{task_id} 或任务不属于你。"
    return f"已放弃任务 #{task.id}「{task.title}」"


def start_task_cmd(store: TaskStore, task_id: int, *, user_id: str = DEFAULT_USER_ID) -> str:
    task = store.start_task(task_id, user_id=user_id)
    if task is None:
        return f"没有找到任务 #{task_id} 或任务不属于你。"
    return f"已开始任务 #{task.id}「{task.title}」"


def reopen_task_cmd(store: TaskStore, task_id: int, *, user_id: str = DEFAULT_USER_ID) -> str:
    task = store.reopen_task(task_id, user_id=user_id)
    if task is None:
        return f"没有找到任务 #{task_id} 或任务不属于你。"
    return f"已恢复任务 #{task.id}「{task.title}」"


def get_user_config_cmd(store: TaskStore, *, user_id: str = DEFAULT_USER_ID) -> str:
    memory = store.get_all_memory(user_id=user_id)
    if not memory:
        return "没有配置项。用 momentum-agent config set <key> <value> 来设置偏好。"
    lines = [f"  {key} = {value}" for key, value in sorted(memory.items())]
    return "当前配置：\n" + "\n".join(lines)


def set_user_config_cmd(store: TaskStore, key: str, value: str, *, user_id: str = DEFAULT_USER_ID) -> str:
    store.set_memory(key, value, user_id=user_id)
    return f"已设置 {key} = {value}"


# ═══════════════════════════════════════════════════════════════════
# Full-fat SDK: multi-agent handoffs, streaming, guardrails, RunConfig
# ═══════════════════════════════════════════════════════════════════






def _make_hooks():
    from agents.lifecycle import RunHooksBase

    class _H(RunHooksBase):
        async def on_tool_start(self, context, agent, tool):
            log.debug("[agent] tool ⚡ %s", tool.name)
        async def on_tool_end(self, context, agent, tool, result):
            log.debug("[agent] tool ✓ %s → %s", tool.name, str(result)[:100])
        async def on_agent_start(self, context, agent):
            log.debug("[agent] ▶ %s", agent.name)
        async def on_agent_end(self, context, agent, output):
            log.debug("[agent] ◼ %s", agent.name)

    return _H()


def _build_agent(store: TaskStore, provider: ProviderConfig, openai_client, *, user_id: str = DEFAULT_USER_ID):
    """Build the unified Momentum agent with handoffs to specialist sub-agents."""
    store_key = str(getattr(store, "db_path", getattr(store, "dsn", str(store))))
    cache_key = (
        store_key,
        user_id,
        provider.model,
        provider.base_url or "",
        provider.api_key or "",
        provider.thinking or "",
        provider.reasoning_effort or "",
        provider.disable_tracing,
    )
    cached = _agent_cache.get(cache_key)
    if cached is not None:
        log.debug("reusing cached agent for user=%r model=%r", user_id, provider.model)
        return cached

    from agents import Agent, OpenAIChatCompletionsModel, function_tool
    from .agents import (
        create_task_tools, create_subtask_tools, create_relation_tools,
        create_weather_tools, create_heartbeat_tools,
        create_insight_tools, create_focus_tools,
    )
    from .agents.tools._common import _to_json

    model = OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client)
    model_settings = build_model_settings(provider)
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    # ── 专家 Agent：洞察与统计 ──
    insight_agent = Agent(
        name="InsightAgent",
        handoff_description="当用户问关于任务统计、完成率、行为洞察、专注推荐、逾期分析、今日/本周任务时，转交给此专家",
        instructions=f"""你是 Momentum 的**洞察分析专家**，专门负责任务数据分析和智能推荐。

## 你的工具
- get_completion_stats — 任务完成统计
- get_behavioral_profile — 用户行为画像
- get_insights — 行为洞察列表
- get_strategic_summary — 战略摘要
- estimate_task_smart — 智能预估任务时间
- get_next_best_task — 推荐最佳任务
- get_tasks_due_today — 今日到期任务
- get_tasks_due_this_week — 本周到期任务
- get_overdue_tasks — 逾期任务
- get_doing_tasks — 进行中任务

## 工作方式
1. 收到问题后，先调相关工具获取数据
2. 可以并行调用多个工具（如 get_completion_stats + get_behavioral_profile）
3. 用数据说话，给出有依据的分析和建议
4. 发现问题（逾期、积压、倦怠）要主动指出

## 沟通风格
- 中文，像朋友聊天
- 用轻量 Markdown 展示数据（表格、列表）
- 给出具体数字和可执行的建议
- 不要长篇大论

## 状态
时间：{now_str}
用户：{user_id}""",
        model=model,
        model_settings=model_settings,
        tools=create_insight_tools(store, user_id) + create_focus_tools(store, user_id),
    )

    # ── 专家 Agent：天气与户外 ──
    weather_agent = Agent(
        name="WeatherAgent",
        handoff_description="当用户问关于天气、户外活动、位置信息时，转交给此专家",
        instructions=f"""你是 Momentum 的**天气与户外活动专家**，帮助用户根据天气规划活动。

## 你的工具
- get_current_weather — 获取当前天气
- plan_outdoor_activity — 规划户外活动
- set_user_location — 设置默认位置
- get_user_location — 获取默认位置
- get_location_info — 获取位置信息

## 工作方式
1. 天气相关问题 → get_current_weather
2. 户外活动规划 → plan_outdoor_activity（会自动结合天气判断）
3. 如果用户没指定城市，使用其保存的默认位置
4. 给出明确的建议：适合/不适合，原因，替代方案

## 沟通风格
- 中文，简洁明了
- 用 emoji 让天气信息更直观
- 给出明确的行动建议

## 状态
时间：{now_str}
用户：{user_id}""",
        model=model,
        model_settings=model_settings,
        tools=create_weather_tools(store, user_id),
    )

    # ── 主 Agent：Momentum ──
    core_tools = (
        create_task_tools(store, user_id)
        + create_subtask_tools(store, user_id)
        + create_relation_tools(store, user_id)
        + create_heartbeat_tools(store, user_id)
    )

    @function_tool
    def get_all_tags() -> str:
        """获取所有标签"""
        return _to_json(store.get_all_tags(user_id=user_id))

    @function_tool
    def get_tasks_by_tag(tag: str) -> str:
        """获取指定标签的任务"""
        tasks = store.get_tasks_by_tag(tag, user_id=user_id)
        return _to_json([{"id": t.id, "title": t.title, "status": t.status.value, "priority": t.priority.value} for t in tasks])

    @function_tool
    def add_tags_to_task(task_id: int, tags: list[str]) -> str:
        """为任务添加标签"""
        task = store._get_task(task_id)
        if not task or (task.user_id and task.user_id != user_id):
            return f"任务 #{task_id} 不存在"
        all_tags = list(set((task.tags or []) + tags))
        updated = store.update_task(task_id, tags=all_tags, user_id=user_id)
        return f"已更新任务 #{updated.id} 的标签" if updated else "更新失败"

    @function_tool
    def save_note(content: str) -> str:
        """保存笔记/偏好"""
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
        from .context import build_user_context
        prefs = _read_preferences(store, user_id=user_id)
        ctx = build_user_context(store.list_tasks(None, user_id=user_id), **prefs)
        return _to_json({"now": ctx.now.isoformat(), "energy": ctx.energy, "available_minutes_today": ctx.available_minutes_today})

    @function_tool
    def get_daily_review() -> str:
        """获取每日回顾"""
        from .context import daily_review as _review
        prefs = _read_preferences(store, user_id=user_id)
        ctx = build_user_context(store.list_tasks(None, user_id=user_id), **prefs)
        return _review(store.list_tasks(None, user_id=user_id), ctx)

    core_tools += [get_all_tags, get_tasks_by_tag, add_tags_to_task, save_note, get_my_notes, get_user_context, get_daily_review]

    agent = Agent(
        name="Momentum",
        instructions=f"""你是 **Momentum**，不是聊天机器人，是一个有自主判断力的任务伙伴。

## 你的工作方式：三步循环

收到任何用户消息后，在心里走这三步：

### 第一步：观察 + 思考（必须先做）
不要急着回复！先用工具搞清楚状况，**明确新任务要先查再做**：
- 用户明确给出要做的事（动作 + 目标，通常带时间/截止）→ 先用 search_tasks 查重：
  - 没有相近任务 → 直接 create_task 或 create_plan
  - 有相近任务 → 询问是否仍需新建（避免重复）
- 即使你在心里查重，也不要对用户说"我先查一下"，直接给出结果/问题。
- 用户说"hi"/"早"/开场白 → 同时调 get_overview + get_user_context（并行）
- 用户提到某个任务 → search_tasks 找到它
- 用户说做了某事 → 先 search_tasks 确认是哪个任务，再 complete_task
- 用户情绪低/说忙/说累 → 拉 get_user_context 了解状态
- 用户提到偏好/习惯 → save_note 记下来，下次记得
- 用户问"接下来做什么"/"该做啥" → 你没有 get_next_best_task 工具，但可以调 get_overview 看看待办任务，手动推荐
- 用户问"今天有什么"/"这周有什么"/"逾期了什么"/"完成率/统计" → **转交给 InsightAgent**
- 用户问天气/户外活动 → **转交给 WeatherAgent**

**关键**：如果用户说了任何涉及"做完了/做了一半/不想做了/推迟"的话，你**必须**找到对应任务并操作。不准只嘴上说"好的"然后什么都不做。

### 第二步：执行（用工具动手）
- 确认要创建的任务 → create_task 或 create_plan
- 确认完成的任务 → complete_task
- 需要开始的 → start_task
- 确认放弃的 → drop_task
- 需要推迟的 → postpone_task
- 工具返回"不存在"时 → 不要放弃，用 search_tasks 模糊搜索找到正确任务

### 第三步：反馈（告诉用户你做了什么，然后主动建议下一步）
- **先给一句操作结果**（比如"已创建任务 #x…"）
- 只给**简短**的概览/建议（除非用户要求详细）
- 如果发现异常（过期/堆积/长期没进展），再补充提醒
- 有进行中的任务时，提醒用户先完成它

## 你的工具清单

**总览：** get_overview — 一次拿到全部状态
**查询：** list_tasks, search_tasks, get_task, get_daily_review, get_user_context
**操作：** create_task, create_plan, edit_task, start_task, complete_task, drop_task, postpone_task, reopen_task
**子任务：** create_subtask, bulk_create_subtasks, get_subtasks, get_task_with_subtasks
**关联：** add_task_dependency, remove_task_dependency, get_task_dependencies, is_task_blocked
**标签：** get_all_tags, get_tasks_by_tag, add_tags_to_task
**批量：** batch_complete_tasks, batch_start_tasks
**记忆：** save_note, get_my_notes
**心跳：** get_system_status, generate_suggestion, get_daily_summary, check_in

## 何时转交给专家 Agent
- **统计/洞察/推荐/逾期/今日/本周任务** → 转交给 InsightAgent
- **天气/户外活动/位置** → 转交给 WeatherAgent

## 必须主动做的事
1. **开场简报** — 用户问候时，先调 get_overview 拿全貌，再给今日总结
2. **识别执行意图** — "搞定了""做完了""交了" → search_tasks + complete_task
3. **发现并指出问题** — 过期任务、长期积压、连续推迟 → 指出来
4. **记住重要信息** — 用户说了偏好/习惯 → save_note
5. **并行调工具** — get_overview + get_user_context 可以一次同时调
6. **主动推荐** — 用户不知道做什么时，用 get_overview 看待办，手动推荐

## 沟通风格
- 中文，像朋友聊天，不机器人腔
- 轻量 Markdown 让信息清晰
- 做了操作要报告，没做操作要说明为什么不
- 信息不足就追问一句，别猜
- 回复简洁，不要长篇大论
- **记住之前的对话内容**，用户提到的任务、偏好、上下文都要记住

## 状态
时间：{now_str}
用户：{user_id}""",
        model=model,
        model_settings=model_settings,
        tools=core_tools,
        handoffs=[insight_agent, weather_agent],
    )
    _agent_cache[cache_key] = agent
    log.info("built and cached agent for user=%r model=%r", user_id, provider.model)
    return agent


async def _build_input_guardrail():
    from agents import input_guardrail, GuardrailFunctionOutput

    @input_guardrail
    async def relevance_check(context, agent, input_text) -> GuardrailFunctionOutput:
        def _extract_role(item: object) -> str | None:
            if isinstance(item, dict):
                role = item.get("role")
            else:
                role = getattr(item, "role", None)
            return str(role).lower() if role else None

        def _extract_content(item: object) -> str | None:
            if isinstance(item, dict):
                content = item.get("content")
            else:
                content = getattr(item, "content", None)

            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text")
                    else:
                        text = getattr(block, "text", None)
                    if text:
                        parts.append(str(text))
                return " ".join(parts).strip() if parts else ""
            if content is not None:
                return str(content).strip()

            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                return str(item["text"]).strip()
            if getattr(item, "text", None):
                return str(getattr(item, "text")).strip()
            return None

        if isinstance(input_text, list):
            user_parts: list[str] = []
            other_parts: list[str] = []
            for item in input_text:
                part = _extract_content(item)
                if part is None:
                    continue
                if _extract_role(item) == "user":
                    user_parts.append(part)
                else:
                    other_parts.append(part)
            if user_parts:
                text = " ".join(user_parts).strip()
            elif other_parts:
                text = " ".join(other_parts).strip()
            else:
                text = ""
            if user_parts:
                if not text or len(text) < 1:
                    return GuardrailFunctionOutput(output_info="empty input", tripwire_triggered=True)
                if len(text) > 10000:
                    return GuardrailFunctionOutput(output_info="input too long", tripwire_triggered=True)
        elif isinstance(input_text, str):
            text = input_text.strip()
            if not text or len(text) < 1:
                return GuardrailFunctionOutput(output_info="empty input", tripwire_triggered=True)
            if len(text) > 10000:
                return GuardrailFunctionOutput(output_info="input too long", tripwire_triggered=True)
        else:
            text = str(input_text).strip() if input_text else ""
            if not text or len(text) < 1:
                return GuardrailFunctionOutput(output_info="empty input", tripwire_triggered=True)
            if len(text) > 10000:
                return GuardrailFunctionOutput(output_info="input too long", tripwire_triggered=True)
        return GuardrailFunctionOutput(output_info="ok", tripwire_triggered=False)

    return relevance_check


async def _build_output_guardrail():
    from agents import output_guardrail, GuardrailFunctionOutput

    @output_guardrail
    async def sanitize_output(context, agent, output) -> GuardrailFunctionOutput:
        if output is None:
            return GuardrailFunctionOutput(output_info="null output masked", tripwire_triggered=True)
        text = str(output)
        if len(text) > 5000:
            return GuardrailFunctionOutput(output_info="output too long", tripwire_triggered=True)
        return GuardrailFunctionOutput(output_info="ok", tripwire_triggered=False)

    return sanitize_output


async def run_agent_message(
    database_url: str, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID
) -> str:
    """Run a message through the full agent system with conversation history."""
    log.info("agent_message user=%r msg=%r has_image=%s", user_id, message[:80], bool(image_base64))
    store = create_task_store(database_url)
    user_config = store.get_all_memory(user_id=user_id)
    provider = load_provider_config(user_config)

    if not provider.is_configured:
        if image_base64:
            return "图片识别功能需要配置 AI 模型。请在 .env 中设置 MOMENTUM_API_KEY。"
        if should_review(message):
            return local_review(store, user_id=user_id)
        if should_plan(message):
            return create_plan_from_text(store, message, user_id=user_id)
        return create_task_from_text(store, message, user_id=user_id)

    try:
        from agents import Runner, RunConfig, set_default_openai_client
    except ImportError:
        return create_task_from_text(store, message, user_id=user_id)
    openai_client = build_openai_client(provider)
    set_default_openai_client(openai_client, use_for_tracing=not provider.disable_tracing)

    agent = _build_agent(store, provider, openai_client, user_id=user_id)
    guardrail = await _build_input_guardrail()
    out_guardrail = await _build_output_guardrail()

    # 构建带历史记录的输入
    history = _get_history(user_id)
    if image_base64:
        agent_input = history + [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": message if message else "请分析这张图片，提取其中的任务信息并创建相应的待办事项。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                ],
            }
        ]
        result = await Runner.run(
            agent, agent_input,
            max_turns=30,
            hooks=_make_hooks(),
            run_config=RunConfig(
                output_guardrails=[out_guardrail],
                workflow_name="momentum-vision",
            ),
        )
    else:
        agent_input = history + [{"role": "user", "content": message}]
        result = await Runner.run(
            agent, agent_input,
            max_turns=30,
            hooks=_make_hooks(),
            run_config=RunConfig(
                input_guardrails=[guardrail],
                output_guardrails=[out_guardrail],
                workflow_name="momentum-chat",
            ),
        )

    reply = result.final_output
    # 保存对话历史
    _save_history(user_id, result.to_input_list())
    log.info("agent done: user=%r len=%d history=%d", user_id, len(reply), len(_get_history(user_id)))
    return reply


async def run_agent_message_stream(
    database_url: str, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID
) -> AsyncIterator[dict]:
    """True streaming with Runner.run_streamed + fallback to Runner.run with hooks.

    Yields dict events:
    - {"type": "tool_start", "name": "search_tasks"}
    - {"type": "tool_end", "name": "search_tasks", "result": "..."}
    - {"type": "chunk", "text": "你好"}
    - {"type": "done"}
    - {"type": "error", "message": "..."}
    """
    log.info("agent_stream user=%r msg=%r has_image=%s", user_id, message[:80], bool(image_base64))
    store = create_task_store(database_url)
    user_config = store.get_all_memory(user_id=user_id)
    provider = load_provider_config(user_config)

    if not provider.is_configured:
        if image_base64:
            yield {"type": "chunk", "text": "图片识别功能需要配置 AI 模型。请在 .env 中设置 MOMENTUM_API_KEY。"}
            yield {"type": "done"}
            return
        if should_review(message):
            yield {"type": "chunk", "text": local_review(store, user_id=user_id)}
        elif should_plan(message):
            yield {"type": "chunk", "text": create_plan_from_text(store, message, user_id=user_id)}
        else:
            yield {"type": "chunk", "text": create_task_from_text(store, message, user_id=user_id)}
        yield {"type": "done"}
        return

    try:
        from agents import Runner, RunConfig, set_default_openai_client
    except ImportError:
        yield {"type": "chunk", "text": create_task_from_text(store, message, user_id=user_id)}
        yield {"type": "done"}
        return
    openai_client = build_openai_client(provider)
    set_default_openai_client(openai_client, use_for_tracing=not provider.disable_tracing)

    agent = _build_agent(store, provider, openai_client, user_id=user_id)
    guardrail = await _build_input_guardrail()
    out_guardrail = await _build_output_guardrail()

    history = _get_history(user_id)
    if image_base64:
        agent_input = history + [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": message if message else "请分析这张图片，提取其中的任务信息并创建相应的待办事项。"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                ],
            }
        ]
        run_config = RunConfig(output_guardrails=[out_guardrail], workflow_name="momentum-vision-stream")
    else:
        agent_input = history + [{"role": "user", "content": message}]
        run_config = RunConfig(input_guardrails=[guardrail], output_guardrails=[out_guardrail], workflow_name="momentum-chat-stream")

    # ── 尝试真流式 ──
    try:
        from agents.stream_events import RunItemStreamEvent, RawResponsesStreamEvent
        result = Runner.run_streamed(agent, agent_input, max_turns=30, hooks=_make_hooks(), run_config=run_config)
        async for event in result.stream_events():
            if isinstance(event, RunItemStreamEvent):
                if event.name == "tool_called":
                    tool_name = getattr(event.item, "tool_name", None) or getattr(event.item, "raw_item", {}).get("name", "tool")
                    yield {"type": "tool_start", "name": str(tool_name)}
                elif event.name == "tool_output":
                    tool_name = getattr(event.item, "tool_name", None) or "tool"
                    yield {"type": "tool_end", "name": str(tool_name)}
            elif isinstance(event, RawResponsesStreamEvent):
                data = event.data
                dtype = type(data).__name__
                # 只推送文本增量（跳过推理增量）
                if hasattr(data, "delta") and data.delta and "TextDelta" in dtype and "Reasoning" not in dtype:
                    yield {"type": "chunk", "text": data.delta}

        if result.is_complete:
            _save_history(user_id, result.to_input_list())
            yield {"type": "done"}
            log.info("agent stream done (real streaming): user=%r", user_id)
            return
    except Exception as exc:
        log.warning("Streaming failed, falling back to Runner.run: %s", exc)

    # ── 回退：Runner.run + hook 事件推送 ──
    event_queue: asyncio.Queue = asyncio.Queue()

    from agents.lifecycle import RunHooksBase

    class _StreamingHooks(RunHooksBase):
        async def on_tool_start(self, context, agent, tool):
            await event_queue.put({"type": "tool_start", "name": tool.name})
        async def on_tool_end(self, context, agent, tool, result):
            await event_queue.put({"type": "tool_end", "name": tool.name})
        async def on_agent_start(self, context, agent):
            pass
        async def on_agent_end(self, context, agent, output):
            pass

    hooks = _StreamingHooks()

    async def _run_agent():
        try:
            result = await Runner.run(agent, agent_input, max_turns=30, hooks=hooks, run_config=run_config)
            await event_queue.put({"type": "_done", "text": result.final_output, "history": result.to_input_list()})
        except Exception as e:
            await event_queue.put({"type": "_error", "message": str(e)})

    task = asyncio.create_task(_run_agent())

    # 从队列中读取事件并推送给前端
    while True:
        try:
            event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            if task.done():
                break
            continue

        if event["type"] == "_done":
            # 流式输出最终文本
            reply = event["text"]
            for char in reply:
                yield {"type": "chunk", "text": char}
                await asyncio.sleep(0.015)
            _save_history(user_id, event.get("history", []))
            yield {"type": "done"}
            break
        elif event["type"] == "_error":
            yield {"type": "error", "message": event["message"]}
            yield {"type": "done"}
            break
        else:
            yield event

    # 确保任务完成
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("agent stream done (fallback): user=%r", user_id)


# 缓存 AsyncOpenAI 客户端，避免每次对话都重建连接池
_openai_client_cache: dict[tuple, object] = {}


def build_openai_client(provider: ProviderConfig):
    from openai import AsyncOpenAI

    base_url = provider.base_url
    api_key = provider.api_key
    if provider.is_ollama:
        base_url = _normalize_ollama_base(base_url)
        api_key = api_key or "ollama"

    key = (api_key or "", base_url or "")
    cached = _openai_client_cache.get(key)
    if cached is not None:
        return cached
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    _openai_client_cache[key] = client
    return client


def _normalize_ollama_base(base_url: str | None) -> str:
    if not base_url:
        return "http://localhost:11434/v1"
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def build_model_settings(provider: ProviderConfig):
    from agents import ModelSettings

    extra_body: dict[str, object] = {}
    if provider.thinking:
        extra_body["thinking"] = {"type": provider.thinking}
    if provider.reasoning_effort:
        extra_body["reasoning_effort"] = provider.reasoning_effort

    return ModelSettings(extra_body=extra_body or None)


def provider_status(user_config: dict[str, str] | None = None) -> dict:
    provider = load_provider_config(user_config)
    if not provider.is_configured:
        return {
            "provider": "Agent provider: local fallback（未配置 API key）",
            "configured": False,
            "provider_type": provider.provider,
        }

    tracing = "disabled" if provider.disable_tracing else "enabled"
    thinking = f" | thinking: {provider.thinking}" if provider.thinking else ""
    effort = f" | reasoning_effort: {provider.reasoning_effort}" if provider.reasoning_effort else ""
    features = " | features: unified agent + session memory + streaming + guardrails"
    user = f" | user: {get_current_user()}"
    label = "Ollama" if provider.is_ollama else provider.provider_label
    return {
        "provider": f"Agent provider: {label} | model: {provider.model}{thinking}{effort}{features}{user} | tracing: {tracing}",
        "configured": True,
        "provider_type": provider.provider,
        "base_url": provider.base_url,
    }


def should_plan(message: str) -> bool:
    plan_markers = ("安排", "规划", "拆分", "计划", "准备", "怎么做")
    return any(marker in message for marker in plan_markers)


def should_review(message: str) -> bool:
    review_markers = ("复盘", "总结", "今天怎么样", "任务状态")
    return any(marker in message for marker in review_markers)


def clear_conversation_history(user_id: str = DEFAULT_USER_ID) -> None:
    """清除用户的对话历史"""
    _clear_history(user_id)
