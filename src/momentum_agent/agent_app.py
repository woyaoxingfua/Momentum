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
from .storage import TaskStore

log = get_logger("agent")

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
    """Build the unified Momentum agent — single agent, all tools, SDK-native session."""
    from agents import Agent, OpenAIChatCompletionsModel
    from .agents import create_agent_tools

    model = OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client)
    tools = create_agent_tools(store, user_id=user_id)
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    return Agent(
        name="Momentum",
        instructions=f"""你是 **Momentum**，不是聊天机器人，是一个有自主判断力的任务伙伴。

## 你的工作方式：三步循环

收到任何用户消息后，在心里走这三步：

### 第一步：观察 + 思考（必须先做）
不要急着回复！先用工具搞清楚状况，**明确新任务要先查再做**：
- 用户明确给出要做的事（动作 + 目标，通常带时间/截止）→ 先用 search_tasks 查重：
  - 没有相近任务 → 直接 create_task 或 create_plan
  - 有相近任务 → 询问是否仍需新建（避免重复）
- 即使你在心里查重，也不要对用户说“我先查一下”，直接给出结果/问题。
- 用户说"hi"/"早"/开场白 → 拉 list_tasks + get_user_context + get_daily_review（可以同时调）
- 用户提到某个任务 → search_tasks 找到它
- 用户说做了某事 → 先 search_tasks 确认是哪个任务，再 complete_task
- 用户情绪低/说忙/说累 → 拉 get_user_context 了解状态
- 用户提到偏好/习惯 → save_note 记下来，下次记得

**关键**：如果用户说了任何涉及"做完了/做了一半/不想做了/推迟"的话，你**必须**找到对应任务并操作。不准只嘴上说"好的"然后什么都不做。

### 第二步：执行（用工具动手）
- 确认要创建的任务 → create_task 或 create_plan
- 确认完成的任务 → complete_task
- 需要开始的 → start_task
- 确认放弃的 → drop_task
- 需要推迟的 → postpone_task

### 第三步：反馈（告诉用户你做了什么，然后主动建议下一步）
- **先给一句操作结果**（比如“已创建任务 #x…”）
- 只给**简短**的概览/建议（除非用户要求详细）
- 如果发现异常（过期/堆积/长期没进展），再补充提醒

## 工具清单（19 个）

**总览：** get_overview — 一次拿到全部状态、过期数、即将到期数、前 3 优先级任务
**查询：** list_tasks, search_tasks, get_daily_review, get_user_context
**操作：** create_task, create_plan, edit_task, start_task, complete_task, drop_task, postpone_task
**标签：** get_all_tags, get_tasks_by_tag, add_tags_to_task
**批量：** batch_complete_tasks, batch_start_tasks
**记忆：** save_note, get_my_notes

## 必须主动做的事

1. **开场简报** — 用户问候时，先调 get_overview 拿全貌，再给今日总结
2. **识别执行意图** — "搞定了""做完了""交了" → search_tasks + complete_task，不只回"好的"
3. **发现并指出问题** — 过期任务、长期积压、连续推迟 → 指出来，建议处理
4. **记住重要信息** — 用户说了偏好/习惯/目标 → save_note，下次对话用 get_my_notes 回顾
5. **并行调工具** — get_overview + get_user_context + get_daily_review 可以一次同时调
6. **模式识别** — 如果你注意到：某任务被反复推迟 → 建议拆分；每天都说"忙" → 建议减少任务量；过期任务堆积 → 建议集中清理

## 沟通风格
- 中文，像朋友聊天，不机器人腔
- 轻量 Markdown 让信息清晰
- 做了操作要报告，没做操作要说明为什么不
- 信息不足就追问一句，别猜

## 状态
时间：{now_str}
用户：{user_id}""",
        model=model,
        model_settings=build_model_settings(provider),
        tools=tools,
    )


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
    db_path: Path, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID
) -> str:
    """Run a message through the full agent system (CLI and web non-streaming).
    
    Supports image_base64 for vision tasks: pass a base64-encoded JPEG/PNG image
    and the agent will analyze it and extract tasks from the image.
    """
    log.info("agent_message user=%r msg=%r has_image=%s", user_id, message[:80], bool(image_base64))
    store = TaskStore(db_path)
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

    # 图片识别：构造多模态消息
    if image_base64:
        agent_input = [
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
        result = await Runner.run(
            agent, message,
            max_turns=30,
            hooks=_make_hooks(),
            run_config=RunConfig(
                input_guardrails=[guardrail],
                output_guardrails=[out_guardrail],
                workflow_name="momentum-chat",
            ),
        )

    reply = result.final_output
    log.info("agent done: user=%r len=%d", user_id, len(reply))
    return reply


async def run_agent_message_stream(
    db_path: Path, message: str, *, image_base64: str | None = None, user_id: str = DEFAULT_USER_ID
) -> AsyncIterator[str]:
    """Stream agent response by first running non-streamed and then yielding chunks manually.

    This completely avoids Agents SDK's history management issues.
    """
    log.info("agent_stream user=%r msg=%r has_image=%s", user_id, message[:80], bool(image_base64))
    store = TaskStore(db_path)
    user_config = store.get_all_memory(user_id=user_id)
    provider = load_provider_config(user_config)

    if not provider.is_configured:
        if image_base64:
            yield "图片识别功能需要配置 AI 模型。请在 .env 中设置 MOMENTUM_API_KEY。"
            return
        if should_review(message):
            yield local_review(store, user_id=user_id)
        elif should_plan(message):
            yield create_plan_from_text(store, message, user_id=user_id)
        else:
            yield create_task_from_text(store, message, user_id=user_id)
        return

    try:
        from agents import Runner, RunConfig, set_default_openai_client
    except ImportError:
        yield create_task_from_text(store, message, user_id=user_id)
        return
    openai_client = build_openai_client(provider)
    set_default_openai_client(openai_client, use_for_tracing=not provider.disable_tracing)

    agent = _build_agent(store, provider, openai_client, user_id=user_id)
    guardrail = await _build_input_guardrail()
    out_guardrail = await _build_output_guardrail()

    # 完全使用非流式方式运行，然后手动分段输出
    if image_base64:
        agent_input = [
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
                workflow_name="momentum-vision-stream",
            ),
        )
    else:
        result = await Runner.run(
            agent, message,
            max_turns=30,
            hooks=_make_hooks(),
            run_config=RunConfig(
                input_guardrails=[guardrail],
                output_guardrails=[out_guardrail],
                workflow_name="momentum-chat-stream",
            ),
        )
    
    reply = result.final_output
    log.info("agent done: user=%r len=%d", user_id, len(reply))
    
    # 手动分段输出，模拟流式效果 - 更自然的分段策略
    import re
    # 根据标点符号和换行来分段，让输出更自然
    chunks = []
    current = ""
    for char in reply:
        current += char
        # 在标点符号或一定长度后分段
        if len(current) >= 10 or char in '，。！？、；："\'）】』》」』、\n':
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    
    # 如果没有合适的分段点，就按字符输出
    if not chunks:
        for char in reply:
            yield char
            await asyncio.sleep(0.02)
    else:
        for chunk in chunks:
            yield chunk
            await asyncio.sleep(0.03)


def build_openai_client(provider: ProviderConfig):
    from openai import AsyncOpenAI

    kwargs = {"api_key": provider.api_key}
    if provider.base_url:
        kwargs["base_url"] = provider.base_url
    return AsyncOpenAI(**kwargs)


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
        }

    tracing = "disabled" if provider.disable_tracing else "enabled"
    thinking = f" | thinking: {provider.thinking}" if provider.thinking else ""
    effort = f" | reasoning_effort: {provider.reasoning_effort}" if provider.reasoning_effort else ""
    features = " | features: unified agent + session memory + streaming + guardrails"
    user = f" | user: {get_current_user()}"
    return {
        "provider": f"Agent provider: {provider.provider_label} | model: {provider.model}{thinking}{effort}{features}{user} | tracing: {tracing}",
        "configured": True,
    }


def should_plan(message: str) -> bool:
    plan_markers = ("安排", "规划", "拆分", "计划", "准备", "怎么做")
    return any(marker in message for marker in plan_markers)


def should_review(message: str) -> bool:
    review_markers = ("复盘", "总结", "今天怎么样", "任务状态")
    return any(marker in message for marker in review_markers)
