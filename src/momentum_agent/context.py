from __future__ import annotations

from datetime import datetime, timedelta

from .models import Task, UserContext


def build_user_context(
    tasks: list[Task],
    *,
    now: datetime | None = None,
    daily_capacity_minutes: int | None = None,
    working_hours_start: str | None = None,
    working_hours_end: str | None = None,
) -> UserContext:
    current = now or datetime.now().astimezone()
    overdue_count = sum(1 for task in tasks if task.due_at and task.due_at < current)
    large_open_count = sum(1 for task in tasks if (task.estimated_minutes or 0) >= 90)

    if overdue_count >= 3:
        pattern = "recently accumulates overdue tasks; recommend smaller next actions"
    elif large_open_count >= 2:
        pattern = "has several large tasks; break them into 20-30 minute steps"
    else:
        pattern = "stable workload; keep recommendations concise"

    available = daily_capacity_minutes or 45
    energy = _estimate_energy(current, working_hours_start, working_hours_end)

    return UserContext(
        now=current,
        energy=energy,
        available_minutes_today=available,
        recent_pattern=pattern,
    )


def _estimate_energy(now: datetime, start: str | None, end: str | None) -> str:
    if not start or not end:
        return "medium"
    try:
        start_h, start_m = map(int, start.split(":"))
        end_h, end_m = map(int, end.split(":"))
    except (ValueError, AttributeError):
        return "medium"

    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if current_minutes < start_minutes:
        return "low"
    if current_minutes > end_minutes - 60:
        return "low"
    if current_minutes < start_minutes + 120:
        return "high"
    return "medium"


def choose_next_action(tasks: list[Task], context: UserContext) -> str:
    if not tasks:
        return "今天没有待办。可以先补充一个最想推进的任务。"

    task = ranked_tasks(tasks, context)[0]
    minutes = task.estimated_minutes or 25

    if task.due_at and task.due_at < context.now:
        return f"「{task.title}」已经过期，建议今天先处理或重新设定截止时间。"

    if minutes > context.available_minutes_today:
        return f"先推进「{task.title}」的一个 20 分钟版本：只做最小可交付的一步。"

    if task.parent_task_id is None and any(child.parent_task_id == task.id for child in tasks):
        child = next(child for child in ranked_tasks(tasks, context) if child.parent_task_id == task.id)
        return f"「{task.title}」比较大，今天先做子任务「{child.title}」。"

    return f"今天优先做「{task.title}」，预计 {minutes} 分钟内完成一个明确进展。"


def ranked_tasks(tasks: list[Task], context: UserContext) -> list[Task]:
    return sorted(tasks, key=lambda task: (-task_score(task, context), task.due_at or context.now, task.id))


def task_score(task: Task, context: UserContext) -> int:
    score = 0
    score += {"high": 30, "medium": 15, "low": 5}.get(task.priority.value, 15)

    if task.due_at:
        delta = task.due_at - context.now
        if delta.total_seconds() < 0:
            score += 80
        elif delta <= timedelta(hours=24):
            score += 55
        elif delta <= timedelta(days=3):
            score += 35
        elif delta <= timedelta(days=7):
            score += 18
    else:
        score -= 8

    minutes = task.estimated_minutes or 25
    if minutes <= context.available_minutes_today:
        score += 12
    elif minutes >= 90:
        score -= 10

    if task.parent_task_id is not None:
        score += 10

    return score


def daily_review(tasks: list[Task], context: UserContext) -> str:
    if not tasks:
        return "今天没有开放任务。建议补一个最重要的小目标，控制在 30 分钟内。"

    overdue = [task for task in tasks if task.due_at and task.due_at < context.now]
    due_soon = [
        task
        for task in tasks
        if task.due_at and context.now <= task.due_at <= context.now + timedelta(days=2)
    ]
    large = [task for task in tasks if (task.estimated_minutes or 0) >= 90]
    next_task = ranked_tasks(tasks, context)[0]

    lines = [
        f"开放任务 {len(tasks)} 个，过期 {len(overdue)} 个，48 小时内到期 {len(due_soon)} 个。",
        f"建议先处理「{next_task.title}」。",
    ]
    if overdue:
        lines.append("过期任务不要硬扛，先决定：今天处理、改截止时间，或直接放弃。")
    if large:
        lines.append("大任务需要拆成 20-30 分钟动作，否则容易拖延。")
    if not overdue and not large:
        lines.append("当前节奏可控，保持每天只推进 1-2 个关键任务。")
    return "\n".join(lines)


def priority_rank(priority: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(priority, 1)
