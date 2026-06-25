from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def create_focus_tools(store: 'TaskStore', user_id: str):
    from ...context import build_user_context, ranked_tasks, task_score
    from ...models import TaskStatus
    from ._common import _to_json, _task_brief, _read_preferences

    @function_tool
    def get_next_best_task() -> str:
        """获取当前最推荐执行的任务。

        基于优先级、截止时间、用户精力和可用时间综合评分，
        返回最值得现在做的单个任务及推荐理由。
        """
        tasks = store.list_tasks(TaskStatus.TODO, user_id=user_id)
        doing = store.list_tasks(TaskStatus.DOING, user_id=user_id)
        all_open = tasks + doing

        if not all_open:
            return _to_json({"task": None, "reason": "当前没有待办任务，可以休息或规划新目标。"})

        prefs = _read_preferences(store, user_id)
        context = build_user_context(all_open, **prefs)
        ranked = ranked_tasks(all_open, context)
        best = ranked[0]

        score = task_score(best, context)
        reason_parts = []

        if best.due_at and best.due_at < context.now:
            days = (context.now - best.due_at).days
            reason_parts.append(f"已逾期{days}天" if days > 0 else "今天到期")
        elif best.due_at:
            hours_left = (best.due_at - context.now).total_seconds() / 3600
            if hours_left <= 24:
                reason_parts.append("24小时内到期")
            elif hours_left <= 72:
                reason_parts.append("3天内到期")

        reason_parts.append(f"优先级：{best.priority.value}")
        if best.estimated_minutes:
            reason_parts.append(f"预估{best.estimated_minutes}分钟")

        if doing:
            reason_parts.append(f"注意：你有{len(doing)}个进行中的任务")

        return _to_json({
            "task": {
                "id": best.id,
                "title": best.title,
                "priority": best.priority.value,
                "due_at": best.due_at.isoformat() if best.due_at else None,
                "estimated_minutes": best.estimated_minutes,
                "status": best.status.value,
                "parent_task_id": best.parent_task_id,
            },
            "score": score,
            "energy": context.energy,
            "available_minutes": context.available_minutes_today,
            "reason": "；".join(reason_parts),
        })

    @function_tool
    def get_tasks_due_today() -> str:
        """获取今天到期的任务列表。"""
        from datetime import datetime

        now = datetime.now().astimezone()
        end_of_day = now.replace(hour=23, minute=59, second=59)
        all_tasks = store.list_tasks(status=None, user_id=user_id)

        result = []
        for t in all_tasks:
            if t.status in (TaskStatus.DONE, TaskStatus.DROPPED):
                continue
            if t.due_at and now <= t.due_at <= end_of_day:
                result.append(_task_brief(t))
        return _to_json(result)

    @function_tool
    def get_tasks_due_this_week() -> str:
        """获取本周到期的任务列表。"""
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        end_of_week = now + timedelta(days=7)
        all_tasks = store.list_tasks(status=None, user_id=user_id)

        result = []
        for t in all_tasks:
            if t.status in (TaskStatus.DONE, TaskStatus.DROPPED):
                continue
            if t.due_at and now <= t.due_at <= end_of_week:
                result.append(_task_brief(t))
        return _to_json(result)

    @function_tool
    def get_overdue_tasks() -> str:
        """获取所有逾期未完成的任务。"""
        from datetime import datetime

        now = datetime.now().astimezone()
        all_tasks = store.list_tasks(status=None, user_id=user_id)

        result = []
        for t in all_tasks:
            if t.status in (TaskStatus.DONE, TaskStatus.DROPPED):
                continue
            if t.due_at and t.due_at < now:
                days = (now - t.due_at).days
                brief = _task_brief(t)
                brief["overdue_days"] = days
                result.append(brief)
        result.sort(key=lambda x: x.get("overdue_days", 0), reverse=True)
        return _to_json(result)

    @function_tool
    def get_completion_stats(days: int = 7) -> str:
        """获取任务完成统计。

        Args:
            days: 统计天数（默认7天）
        """
        from datetime import datetime, timedelta

        now = datetime.now().astimezone()
        since = now - timedelta(days=days)
        all_tasks = store.list_tasks(status=None, user_id=user_id)

        created_in_range = [t for t in all_tasks if t.created_at >= since]
        done_tasks = [t for t in all_tasks if t.status == TaskStatus.DONE and t.updated_at >= since]
        dropped_tasks = [t for t in all_tasks if t.status == TaskStatus.DROPPED and t.updated_at >= since]
        still_open = [t for t in all_tasks if t.status in (TaskStatus.TODO, TaskStatus.DOING)]

        done_by_priority = {"high": 0, "medium": 0, "low": 0}
        for t in done_tasks:
            done_by_priority[t.priority.value] = done_by_priority.get(t.priority.value, 0) + 1

        total_estimated = sum(t.estimated_minutes or 0 for t in done_tasks)
        avg_estimated = total_estimated / len(done_tasks) if done_tasks else 0

        completion_rate = len(done_tasks) / len(created_in_range) if created_in_range else 0.0

        return _to_json({
            "period_days": days,
            "created": len(created_in_range),
            "completed": len(done_tasks),
            "dropped": len(dropped_tasks),
            "still_open": len(still_open),
            "completion_rate": round(completion_rate, 2),
            "done_by_priority": done_by_priority,
            "avg_estimated_minutes": round(avg_estimated),
            "total_estimated_minutes": total_estimated,
        })

    @function_tool
    def get_doing_tasks() -> str:
        """获取所有进行中的任务。"""
        tasks = store.list_tasks(TaskStatus.DOING, user_id=user_id)
        return _to_json([_task_brief(t) for t in tasks])

    return [
        get_next_best_task,
        get_tasks_due_today,
        get_tasks_due_this_week,
        get_overdue_tasks,
        get_completion_stats,
        get_doing_tasks,
    ]
