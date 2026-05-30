from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULT_USER_ID, ProviderConfig, get_current_user, load_provider_config
from .context import build_user_context, choose_next_action, daily_review
from .logger import get_logger
from .models import ParsedTaskOutput, PlanOutput, Priority, TaskStatus
from .parser import ParsedTask, parse_task_text
from .planner import create_task_plan
from .storage import TaskStore

log = get_logger("agent")


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


async def _parse_task_with_ai_and_images(text: str, images: list[str], provider: ProviderConfig) -> ParsedTask:
    """使用视觉模型从图片中提取任务"""
    from agents import Agent, OpenAIChatCompletionsModel, Runner
    
    openai_client = build_openai_client(provider)
    agent = Agent(
        name="Task Extractor from Images",
        instructions="""
你是一个任务提取专家。请仔细分析用户提供的图片，提取其中的任务信息。

任务提取规则：
1. title：从图片中识别出要完成的任务或待办事项
2. due_at：如果图片中包含日期或时间信息，解析为 ISO 8601 格式（YYYY-MM-DDTHH:MM:SS）
3. priority：如果图片中标注了紧急/重要/优先等关键词，设置为 "high"
4. estimated_minutes：如果图片中标注了时间，转换为分钟数
5. notes：记录图片中的额外信息（如来源、背景等）
6. 如果图片中没有明确的任务，从图片内容推断一个合理的任务

请用中文理解和输出。
""",
        model=OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client),
        output_type=ParsedTask,
    )
    
    # 构建消息内容
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for img_base64 in images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}})
    
    result = await Runner.run(agent, [{"role": "user", "content": content}])
    return result.final_output_as(ParsedTask)


def create_task_from_text(store: TaskStore, text: str, *, user_id: str = DEFAULT_USER_ID, images: list[str] | None = None) -> str:
    log.info("create_task_from_text user=%r text=%r has_images=%s", user_id, text[:80] if text else "", bool(images))
    user_config = store.get_all_memory(user_id=user_id)
    provider = load_provider_config(user_config)
    
    vision_enabled = user_config.get("vision_enabled", "false") == "true"
    
    # 如果有图片，检查用户是否启用了视觉功能
    if images and provider.is_configured:
        if vision_enabled:
            try:
                parsed = asyncio.run(_parse_task_with_ai_and_images(text, images, provider))
                return _parsed_to_message(parsed, store, user_id=user_id)
            except Exception as exc:
                log.warning("AI vision parse failed, falling back to regex: %s", exc)
                if not text:
                    return "抱歉，AI 识别图片失败了。请手动输入任务内容。"
                parsed = parse_task_text(text)
        else:
            log.warning("Vision not enabled by user, skipping image processing")
            return "抱歉，您当前未启用视觉功能。请在偏好设置中开启「启用视觉功能」选项后再上传图片。"
    
    if provider.is_configured:
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


async def _parse_task_with_ai(text: str, provider: ProviderConfig) -> ParsedTask:
    from agents import Agent, OpenAIChatCompletionsModel, Runner

    openai_client = build_openai_client(provider)
    agent = Agent(
        name="Task Parser",
        instructions="""
You are a precise task parser. Extract structured task information from the user's natural language input.

Rules:
1. title: Remove date words (今天/明天/后天/下周), time words (上午/中午/下午/晚上), priority words (紧急/重要/尽快/必须/马上/有空/不急/随便), and conversational prefixes (帮我/记一下/提醒我/我想/需要/安排/规划/计划/拆分). Keep the core action.
2. due_at: Parse relative dates (今天→today, 明天→tomorrow, 后天→day after tomorrow, 下周→next Monday) into ISO 8601. Default to 18:00 if no time specified. 上午→10:00, 中午→12:00, 下午→15:00, 晚上→20:00. Return null if no deadline.
3. priority: "high" for 紧急/重要/必须/马上/尽快. "low" for 有空/不急/随便. Otherwise "medium".
4. estimated_minutes: explicit "N分钟" or "N小时" → convert to minutes. Heuristic: 整理/准备/研究/写 → 45. Return null if unclear.
5. notes: Any extra context not captured above, or null.
""",
        model=OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client),
        output_type=ParsedTaskOutput,
    )
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


def _make_tools(store: TaskStore, *, user_id: str = DEFAULT_USER_ID):
    """Create the full function_tool set for SDK agents, bound to a specific user."""
    from agents import function_tool

    def _to_json(payload: object) -> str:
        return json.dumps(payload, ensure_ascii=False)

    @function_tool
    def create_task(title: str, due_at: str | None = None, priority: str = "medium", notes: str | None = None) -> str:
        """Create a task in the local todo database."""
        parsed = parse_task_text(f"{due_at or ''} {title}")
        chosen_priority = Priority(priority) if priority in Priority._value2member_map_ else parsed.priority
        task = store.create_task(
            title,
            due_at=parsed.due_at,
            priority=chosen_priority,
            estimated_minutes=parsed.estimated_minutes,
            notes=notes,
            user_id=user_id,
        )
        log.info("agent tool create_task: #%d user=%r", task.id, user_id)
        due_info = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
        rec_info = {"daily": "（每天重复）", "weekly": "（每周重复）", "monthly": "（每月重复）"}.get(task.recurrence or "", "")
        return f"已创建任务 #{task.id}：{task.title}{due_info}{rec_info}"

    @function_tool
    def create_plan(text: str) -> str:
        """Create a parent task and practical subtasks from a larger goal."""
        return create_plan_from_text(store, text, user_id=user_id)

    @function_tool
    def list_tasks(status: str = "todo") -> str:
        """List tasks by status (JSON string). Status: todo, doing, done, dropped, or 'all'."""
        if status == "all":
            tasks = store.list_tasks(status=None, user_id=user_id)
        else:
            chosen = TaskStatus(status) if status in TaskStatus._value2member_map_ else TaskStatus.TODO
            tasks = store.list_tasks(chosen, user_id=user_id)
        payload = [
            {"id": t.id, "title": t.title, "status": t.status.value, "priority": t.priority.value,
             "due_at": t.due_at.isoformat() if t.due_at else None,
             "estimated_minutes": t.estimated_minutes,
             "parent_task_id": t.parent_task_id, "recurrence": t.recurrence}
            for t in tasks
        ]
        return _to_json(payload)

    @function_tool
    def get_overview() -> str:
        """Get a task overview as JSON: counts, overdue, due-soon, top-3 todos."""
        all_tasks = store.list_tasks(status=None, user_id=user_id)
        now = datetime.now().astimezone()
        counts = {"todo": 0, "doing": 0, "done": 0, "dropped": 0}
        overdue = 0
        due_soon = 0
        for t in all_tasks:
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
            if t.status in (TaskStatus.TODO, TaskStatus.DOING) and t.due_at:
                if t.due_at < now:
                    overdue += 1
                elif t.due_at < now + timedelta(days=2):
                    due_soon += 1
        payload = {
            "total": len(all_tasks),
            "by_status": counts,
            "overdue": overdue,
            "due_within_48h": due_soon,
            "top_3_todo": [
                {"id": t.id, "title": t.title, "priority": t.priority.value,
                 "due_at": t.due_at.isoformat() if t.due_at else None}
                for t in all_tasks if t.status == TaskStatus.TODO
            ][:3],
        }
        return _to_json(payload)

    @function_tool
    def edit_task(task_id: int, title: str | None = None, due_at: str | None = None,
                  priority: str | None = None, estimated_minutes: int | None = None,
                  notes: str | None = None) -> str:
        """Edit a task's fields. Pass only the fields you want to change. due_at in ISO format."""
        parsed_due = datetime.fromisoformat(due_at) if due_at else None
        parsed_pri = Priority(priority) if priority and priority in Priority._value2member_map_ else None
        task = store.update_task(task_id, title=title, due_at=parsed_due,
                                 priority=parsed_pri, estimated_minutes=estimated_minutes,
                                 notes=notes, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        due = f"，截止 {task.due_at.strftime('%Y-%m-%d %H:%M')}" if task.due_at else ""
        return f"已更新任务 #{task.id}：{task.title}{due}"

    @function_tool
    def get_daily_review() -> str:
        """Get a concise daily review of open task risk and recommended focus."""
        return local_review(store, user_id=user_id)

    @function_tool
    def get_user_context() -> str:
        """Get current workload/energy/available time as JSON."""
        prefs = _read_preferences(store, user_id=user_id)
        context = build_user_context(store.list_tasks(TaskStatus.TODO, user_id=user_id), **prefs)
        payload = {
            "now": context.now.isoformat(),
            "energy": context.energy,
            "available_minutes_today": context.available_minutes_today,
            "recent_pattern": context.recent_pattern,
            "local_advice": choose_next_action(store.list_tasks(TaskStatus.TODO, user_id=user_id), context),
        }
        return _to_json(payload)

    @function_tool
    def complete_task(task_id: int) -> str:
        """Mark a task as done by its ID. Handles recurring tasks automatically.
        Only works on tasks belonging to the current user."""
        task = store._get_task(task_id)
        if not task:
            return f"task #{task_id} not found"
        if task.user_id != user_id:
            return f"task #{task_id} does not belong to you"
        next_task = store.complete_recurring_task(task_id)
        if not next_task:
            return f"任务 #{task_id} 不存在或不属于你"
        if next_task.recurrence:
            return f"已完成 #{task_id}：{next_task.title}，已自动创建下一期任务 #{next_task.id}"
        return f"已完成 #{task_id}：{next_task.title}"

    @function_tool
    def start_task(task_id: int) -> str:
        """Mark a task as in-progress by its ID. Only on current user's tasks."""
        task = store.start_task(task_id, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        return f"已开始 #{task.id}：{task.title}"

    @function_tool
    def drop_task(task_id: int) -> str:
        """Drop/abandon a task by its ID. Only on current user's tasks."""
        task = store.drop_task(task_id, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        return f"已放弃 #{task.id}：{task.title}"

    @function_tool
    def postpone_task(task_id: int, days: int = 3) -> str:
        """Postpone a task by N days. Only on current user's tasks."""
        task = store.postpone_task(task_id, days, user_id=user_id)
        if not task:
            return f"任务 #{task_id} 不存在或不属于你"
        new_due = task.due_at.strftime("%Y-%m-%d") if task.due_at else "无截止"
        return f"已推迟 #{task.id}：{task.title} → {new_due}"

    @function_tool
    def search_tasks(query: str) -> str:
        """Search tasks by keyword in title (JSON string)."""
        payload = [
            {"id": t.id, "title": t.title, "status": t.status.value, "priority": t.priority.value,
             "due_at": t.due_at.isoformat() if t.due_at else None}
            for t in store.search_tasks(query, user_id=user_id)
        ]
        return _to_json(payload)

    @function_tool
    def save_note(content: str) -> str:
        """Save a personal note/observation for future reference. Use this to remember user preferences,
        important context, or decisions that should persist across conversations."""
        store.set_memory(f"agent_note_{int(datetime.now().timestamp())}", content, user_id=user_id)
        return f"note saved: {content[:80]}"

    @function_tool
    def get_my_notes() -> str:
        """Retrieve all notes you've saved about this user (JSON string)."""
        all_mem = store.get_all_memory(user_id=user_id)
        payload = {k: v for k, v in all_mem.items() if k.startswith("agent_note_")}
        return _to_json(payload)

    # ── v1 新增：标签 & 批量操作工具 ──────────────────────────────────

    @function_tool
    def get_all_tags() -> str:
        """Get all tags used across user's tasks (JSON array string)."""
        tags = store.get_all_tags(user_id=user_id)
        return _to_json(tags)

    @function_tool
    def get_tasks_by_tag(tag: str) -> str:
        """Get all tasks with a specific tag (JSON string)."""
        tasks = store.get_tasks_by_tag(tag, user_id=user_id)
        payload = [
            {"id": t.id, "title": t.title, "status": t.status.value,
             "priority": t.priority.value,
             "due_at": t.due_at.isoformat() if t.due_at else None,
             "tags": t.tags}
            for t in tasks
        ]
        return _to_json(payload)

    @function_tool
    def add_tags_to_task(task_id: int, tags: list[str]) -> str:
        """Add one or more tags to a task. Existing tags are preserved."""
        task = store._get_task(task_id)
        if not task or (task.user_id and task.user_id != user_id):
            return f"任务 #{task_id} 不存在或不属于你"
        existing_tags = task.tags or []
        all_tags = list(set(existing_tags + tags))
        updated = store.update_task(task_id, tags=all_tags, user_id=user_id)
        if not updated:
            return f"更新任务 #{task_id} 失败"
        tags_info = f"，标签：{', '.join(updated.tags)}" if updated.tags else ""
        return f"已更新任务 #{updated.id}：{updated.title}{tags_info}"

    @function_tool
    def batch_complete_tasks(task_ids: list[int]) -> str:
        """Mark multiple tasks as done at once. Pass a list of task IDs."""
        count = store.batch_update_status(task_ids, TaskStatus.DONE, user_id=user_id)
        return f"已批量完成 {count} 个任务"

    @function_tool
    def batch_start_tasks(task_ids: list[int]) -> str:
        """Mark multiple tasks as in-progress at once. Pass a list of task IDs."""
        count = store.batch_update_status(task_ids, TaskStatus.DOING, user_id=user_id)
        return f"已批量开始 {count} 个任务"

    return [
        create_task, create_plan, list_tasks, get_overview, get_daily_review, get_user_context,
        complete_task, start_task, drop_task, postpone_task, search_tasks, edit_task,
        save_note, get_my_notes,
        # v1 新增工具
        get_all_tags, get_tasks_by_tag, add_tags_to_task,
        batch_complete_tasks, batch_start_tasks,
    ]


# SDK-native session memory — persisted in SQLite alongside tasks
_sessions: dict[str, object] = {}


SESSION_LIMIT: int | None = None
SESSION_VERSION = "v6"  # 完全禁用持久化历史，仅内存中临时保存，但使用 notes 工具保存记忆


def _extract_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
            else:
                text = getattr(part, "text", None)
            if text:
                parts.append(str(text))
        return "".join(parts)
    return str(content)


def _is_empty_assistant_message(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("role") != "assistant":
        return False
    if item.get("tool_calls"):
        return False
    text = _extract_text(item.get("content", "")).strip()
    return not text


def _sanitize_items(items: list[object]) -> list[object]:
    """
    安全的对话历史清理策略：
    1. 只保留 user 和 assistant 纯文本对话，过滤掉所有 tool_calls 和 tool 响应
    2. 这样虽然会丢失一些上下文，但能保证对话历史永远不会出错！
    """
    result: list[object] = []
    
    for item in items:
        is_dict = isinstance(item, dict)
        role = item.get("role") if is_dict else getattr(item, "role", None)
        tool_calls = item.get("tool_calls") if is_dict else getattr(item, "tool_calls", None)
        
        # 跳过空 assistant 消息
        if is_dict and _is_empty_assistant_message(item):
            continue
        
        # 过滤掉带 tool_calls 的 assistant 消息
        if role == "assistant" and tool_calls:
            continue
        
        # 过滤掉 tool 响应消息
        if role == "tool":
            continue
        
        # 只保留纯 user 和 assistant 文本消息
        result.append(item)
    
    return result


def _cleanup_session_db(db_path: Path, session_id: str) -> None:
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_messages'"
    )
    if not cur.fetchone():
        conn.close()
        return
    cur.execute(
        "SELECT id, message_data FROM agent_messages WHERE session_id = ? ORDER BY id",
        (session_id,),
    )
    rows = cur.fetchall()
    delete_ids: set[int] = set()
    pending_tool_calls: set[str] = set()

    for msg_id, data in rows:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        
        if _is_empty_assistant_message(payload):
            delete_ids.add(msg_id)
            continue
        
        # 检查新的格式：role: assistant, tool_calls: [...]
        role = payload.get("role")
        if role == "assistant" and payload.get("tool_calls"):
            for tc in payload.get("tool_calls", []):
                cid = tc.get("id")
                if cid:
                    pending_tool_calls.add(cid)
            continue
        
        # 检查新格式：role: tool
        if role == "tool":
            tool_call_id = payload.get("tool_call_id")
            if tool_call_id and tool_call_id in pending_tool_calls:
                pending_tool_calls.discard(tool_call_id)
            continue
        
        # 旧格式处理
        item_type = payload.get("type")
        if item_type == "function_call":
            call_id = payload.get("call_id")
            if call_id:
                pending_tool_calls.add(call_id)
            else:
                delete_ids.add(msg_id)
        elif item_type == "function_call_output":
            call_id = payload.get("call_id")
            if call_id and call_id in pending_tool_calls:
                pending_tool_calls.discard(call_id)
            else:
                delete_ids.add(msg_id)
    
    # 如果还有 pending 的 calls，直接清空整个 session 更安全
    if pending_tool_calls:
        log.warning("检测到不完整的对话历史，清空整个 session")
        cur.execute("DELETE FROM agent_messages WHERE session_id = ?", (session_id,))
    elif delete_ids:
        cur.executemany(
            "DELETE FROM agent_messages WHERE id = ?",
            [(msg_id,) for msg_id in sorted(delete_ids)],
        )
    
    conn.commit()
    conn.close()


def _get_session(db_path: Path, user_id: str):
    """
    完全禁用对话历史持久化，每次都是全新会话！
    但 Agent 可以用 save_note/get_my_notes 工具记住你的偏好。
    """
    from agents import Session
    from agents import SessionSettings
    
    # 每次都返回一个全新的内存会话
    return Session(
        session_id=f"{user_id}-{SESSION_VERSION}-{int(datetime.now().timestamp())}",
        session_settings=SessionSettings(limit=0),
    )


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

    model = OpenAIChatCompletionsModel(model=provider.model, openai_client=openai_client)
    tools = _make_tools(store, user_id=user_id)
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
    """Stream agent response via Runner.run_streamed() with session + hooks + guardrails.

    Supports image_base64 for vision tasks. When image is provided, runs non-streamed
    and yields the result line by line.
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

    # 图片识别：非流式处理后逐行 yield
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
        result_vision = await Runner.run(
            agent, agent_input,
            max_turns=30,
            hooks=_make_hooks(),
            run_config=RunConfig(
                output_guardrails=[out_guardrail],
                workflow_name="momentum-vision-stream",
            ),
        )
        reply = result_vision.final_output
        log.info("agent_vision done: user=%r len=%d", user_id, len(reply))
        yield reply
        return

    result = Runner.run_streamed(
        agent, message,
        max_turns=30,
        hooks=_make_hooks(),
        run_config=RunConfig(
            input_guardrails=[guardrail],
            output_guardrails=[out_guardrail],
            workflow_name="momentum-chat-stream",
        ),
    )
    full_reply = ""
    chunk_count = 0
    async for event in result.stream_events():
        if event.type == "run_item_stream_event":
            item = event.item
            if item.type == "message_output_item":
                if hasattr(item, "raw_item") and hasattr(item.raw_item, "content"):
                    for block in item.raw_item.content:
                        if hasattr(block, "text"):
                            chunk_count += 1
                            full_reply += block.text
                            yield block.text
            elif item.type == "reasoning_item":
                pass
        elif event.type == "raw_response_event":
            if hasattr(event.data, "delta") and hasattr(event.data.delta, "content"):
                for block in event.data.delta.content:
                    if hasattr(block, "text") and block.text:
                        chunk_count += 1
                        full_reply += block.text
                        yield block.text
    log.info("agent_stream done: user=%r chunks=%d", user_id, chunk_count)


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
