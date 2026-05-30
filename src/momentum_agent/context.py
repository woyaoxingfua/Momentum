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
        return "💫 今天没有待办！这是个很棒的状态，建议花 5 分钟规划一个最想推进的小目标。"

    task = ranked_tasks(tasks, context)[0]
    minutes = task.estimated_minutes or 25

    # 场景化建议
    if task.due_at and task.due_at < context.now:
        overdue_days = (context.now - task.due_at).days
        if overdue_days <= 1:
            return f"⚠️ 「{task.title}」刚过期，今天花 {minutes} 分钟处理完它，或者重新设定一个合理的截止时间。"
        else:
            return f"⏰ 「{task.title}」已过期 {overdue_days} 天，建议：要么立即处理，要么果断调整截止时间或放弃。"
    
    # 检查是否有正在进行的任务
    doing_tasks = [t for t in tasks if t.status.value == "doing"]
    if doing_tasks:
        return f"🔄 你有进行中的任务「{doing_tasks[0].title}」，建议先完成它再开始新任务，保持专注！"

    # 大任务建议
    if minutes > context.available_minutes_today:
        return f"📐 「{task.title}」估计需要 {minutes} 分钟，今天先做一个 20 分钟的简化版本：只完成最核心的一步。"

    # 父子任务建议
    if task.parent_task_id is None and any(child.parent_task_id == task.id for child in tasks):
        child = next(child for child in ranked_tasks(tasks, context) if child.parent_task_id == task.id)
        return f"📋 「{task.title}」是个大任务，从子任务「{child.title}」开始吧！这样更容易获得成就感。"
    
    # 根据时间段给出建议
    hour = context.now.hour
    time_suggestion = ""
    if hour < 10:
        time_suggestion = "清晨是处理困难任务的好时机！"
    elif hour < 12:
        time_suggestion = "上午精力充沛，适合专注攻坚！"
    elif hour < 14:
        time_suggestion = "午间时间，可以处理一些轻松的任务。"
    elif hour < 17:
        time_suggestion = "下午状态不错，继续保持节奏！"
    else:
        time_suggestion = "晚上时间，适合做一些整理或回顾工作。"

    return f"✅ {time_suggestion}\n今天优先做「{task.title}」，预计 {minutes} 分钟就能看到明确进展。"


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
        return "✨ 太棒了！当前没有开放任务。\n💡 建议：花 10 分钟规划一个明天最想完成的小目标，或者享受这段闲暇时光！"

    overdue = [task for task in tasks if task.due_at and task.due_at < context.now]
    due_soon = [
        task
        for task in tasks
        if task.due_at and context.now <= task.due_at <= context.now + timedelta(days=2)
    ]
    due_today = [
        task for task in tasks 
        if task.due_at and task.due_at.date() == context.now.date()
    ]
    large = [task for task in tasks if (task.estimated_minutes or 0) >= 90]
    high_priority = [task for task in tasks if task.priority.value == "high"]
    next_task = ranked_tasks(tasks, context)[0]
    
    # 统计分析
    total_tasks = len(tasks)
    todo_count = len([t for t in tasks if t.status.value == "todo"])
    doing_count = len([t for t in tasks if t.status.value == "doing"])
    
    lines = []
    
    # 总体概览
    lines.append(f"📊 任务概览：共 {total_tasks} 个开放任务")
    if todo_count > 0:
        lines.append(f"   • 待办：{todo_count} 个")
    if doing_count > 0:
        lines.append(f"   • 进行中：{doing_count} 个")
    if overdue:
        lines.append(f"   • 已过期：{len(overdue)} 个 ⚠️")
    if due_soon:
        lines.append(f"   • 48小时内到期：{len(due_soon)} 个")
    if high_priority:
        lines.append(f"   • 高优先级：{len(high_priority)} 个")
    
    lines.append("")  # 空行
    
    # 今日重点建议
    if due_today:
        lines.append(f"🎯 今日重点：有 {len(due_today)} 个任务今天到期")
        if len(due_today) <= 3:
            lines.append(f"   分别是：{', '.join([f'「{t.title}」' for t in due_today])}")
        else:
            lines.append(f"   建议先完成「{due_today[0].title}」")
    
    if overdue:
        lines.append("")
        lines.append("⚠️ 关于过期任务：")
        lines.append("   过期任务不用焦虑，可以：")
        lines.append("   1. 挑选 1-2 个今天立即处理")
        lines.append("   2. 重新设定更合理的截止时间")
        lines.append("   3. 如果不再重要，果断放弃")
    
    if large:
        lines.append("")
        lines.append("📐 关于大任务：")
        lines.append(f"   有 {len(large)} 个任务估计超过 90 分钟")
        lines.append("   建议拆分成 20-30 分钟的小步骤，避免拖延")
    
    if not overdue and not large and total_tasks <= 5:
        lines.append("")
        lines.append("🎉 状态不错！任务数量和紧急程度都可控")
        lines.append("   保持专注，每天只推进 1-2 个关键任务就很好")
    
    lines.append("")
    lines.append(f"🚀 下一步建议：先从「{next_task.title}」开始")
    
    # 时间相关建议
    hour = context.now.hour
    if hour < 10:
        lines.append("   清晨思路清晰，适合处理有挑战的任务")
    elif hour < 12:
        lines.append("   上午精力充沛，是高效工作的黄金时段")
    elif hour < 14:
        lines.append("   午间可以处理一些轻松或需要沟通的任务")
    elif hour < 17:
        lines.append("   下午状态稳定，继续保持专注")
    else:
        lines.append("   晚上适合整理、回顾或做一些轻松的收尾工作")
    
    return "\n".join(lines)


def priority_rank(priority: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(priority, 1)


def heartbeat_suggestion(tasks: list[Task], context: UserContext) -> str:
    """Generate a friendly, proactive heartbeat suggestion for the user.

    This should be a natural, engaging message that makes the user want to
    continue with their tasks without feeling pressured.
    """
    if not tasks:
        return (
            "👋 嘿！现在是个好时机来规划一下今天要做什么。\n"
            "要不要花 2 分钟创建一个今天最想推进的小任务？"
        )

    overdue = [task for task in tasks if task.due_at and task.due_at < context.now]
    due_today = [
        task
        for task in tasks
        if task.due_at and task.due_at.date() == context.now.date()
    ]
    doing_tasks = [task for task in tasks if task.status.value == "doing"]
    ranked = ranked_tasks(tasks, context)
    next_task = ranked[0]

    hour = context.now.hour

    # Time-based greetings
    if hour < 12:
        greeting = "☀️ 早上好！"
    elif hour < 17:
        greeting = "🌤️ 下午好！"
    else:
        greeting = "🌙 晚上好！"

    suggestions = []

    # Different suggestion strategies
    if overdue:
        suggestions.append(
            f"看到有 {len(overdue)} 个任务过期了，没关系！"
            f"要不要先快速处理一下「{overdue[0].title}」？"
        )
    elif doing_tasks:
        suggestions.append(
            f"你有正在进行的任务「{doing_tasks[0].title}」，"
            f"要不要继续推进它？预计还需要 {doing_tasks[0].estimated_minutes or 25} 分钟。"
        )
    elif due_today:
        suggestions.append(
            f"今天有 {len(due_today)} 个任务要完成，"
            f"「{due_today[0].title}」是个不错的起点！"
        )
    elif next_task.priority.value == "high":
        suggestions.append(
            f"高优先级任务「{next_task.title}」在等你，"
            f"预计需要 {next_task.estimated_minutes or 25} 分钟，现在开始正好！"
        )
    else:
        suggestions.append(
            f"从「{next_task.title}」开始怎么样？"
            f"预计 {next_task.estimated_minutes or 25} 分钟就能看到进展。"
        )

    # Add energy-based suggestion
    if context.energy == "high":
        suggestions.append("现在精力正好，适合处理有挑战的任务！")
    elif context.energy == "medium":
        suggestions.append("当前状态不错，保持节奏就好。")
    else:
        suggestions.append("累了就先休息，或者选个 15 分钟的轻松小任务。")

    # Add task count summary
    todo_count = len([t for t in tasks if t.status.value == "todo"])
    if todo_count > 5:
        suggestions.append(f"有 {todo_count} 个待办，别着急，一个一个来。")
    elif todo_count == 0:
        suggestions.append("待办清零了！很棒！要不要规划一下明天？")

    # Combine greeting with first suggestion and a friendly prompt
    main_suggestion = suggestions[0]
    extra = suggestions[1] if len(suggestions) > 1 else ""

    return f"{greeting} {main_suggestion}\n\n{extra}".strip()
