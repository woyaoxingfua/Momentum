from typing import TYPE_CHECKING
from agents import function_tool

if TYPE_CHECKING:
    from ...storage import TaskStore


def create_heartbeat_tools(store: "TaskStore", user_id: str):
    from ...services import heartbeat as hb
    from ._common import _to_json

    @function_tool
    def get_system_status() -> str:
        """获取系统当前状态概览：待办、进行中、已完成、逾期、今日到期。"""
        return _to_json(hb.stats(store, user_id))

    @function_tool
    def get_daily_summary() -> str:
        """获取今日任务摘要和需要关注的任务。"""
        from datetime import datetime

        s = hb.stats(store, user_id)
        now = datetime.now().astimezone()
        end_of_today = now.replace(hour=23, minute=59, second=59)
        today = [
            t for t in store.list_tasks(status=None, user_id=user_id)
            if t.due_at and t.due_at <= end_of_today and t.status.value in ("todo", "doing")
        ]
        today_titles = [f"#{t.id} {t.title}" for t in today[:10]]
        lines = [
            f"共 {s['total']} 个任务，待办 {s['todo']}，进行中 {s['doing']}，完成 {s['done']}",
            f"逾期 {s['overdue']}，今日到期 {s['upcoming_24h']}",
        ]
        if today_titles:
            lines.append("今日待办：" + "；".join(today_titles))
        return _to_json({
            "stats": s,
            "today_count": len(today),
            "today_tasks": today_titles,
            "summary": "\n".join(lines),
        })

    @function_tool
    def check_in() -> str:
        """签到，记录一次心跳并返回鼓励语。"""
        hb.update_last_heartbeat(store, user_id)
        s = hb.stats(store, user_id)
        if s["done"] > 0 and s["todo"] == 0:
            msg = "今天的任务都完成了，干得漂亮！"
        elif s["overdue"] > 0:
            msg = f"有 {s['overdue']} 个任务逾期了，先挑一个搞定吧。"
        else:
            msg = "继续加油，一件一件来。"
        return _to_json({"ok": True, "message": msg, "stats": s})

    return [get_system_status, get_daily_summary, check_in]
