from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from momentum_agent.context import build_user_context, choose_next_action, daily_review
from momentum_agent.models import Priority
from momentum_agent.planner import create_task_plan
from momentum_agent.storage import TaskStore


def test_advice_prefers_overdue_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    now = datetime(2026, 5, 25, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    store.create_task("交水费", due_at=now - timedelta(days=1), priority=Priority.HIGH)
    store.create_task("整理资料", due_at=now + timedelta(days=2), priority=Priority.MEDIUM)

    tasks = store.list_tasks()
    context = build_user_context(tasks, now=now)
    advice = choose_next_action(tasks, context)

    assert "交水费" in advice
    assert "已经过期" in advice


def test_plan_creates_parent_and_children(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    parent, children = create_task_plan(store, "下周准备产品经理面试")

    assert "准备产品经理面试" in parent.title
    assert len(children) == 3
    assert all(child.parent_task_id == parent.id for child in children)


def test_daily_review_mentions_risk_counts(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    now = datetime(2026, 5, 25, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    store.create_task("过期任务", due_at=now - timedelta(days=1), priority=Priority.HIGH)
    store.create_task("近期任务", due_at=now + timedelta(days=1), priority=Priority.MEDIUM)

    review = daily_review(store.list_tasks(), build_user_context(store.list_tasks(), now=now))

    assert "开放任务 2 个" in review
    assert "过期 1 个" in review
    assert "48 小时内到期 1 个" in review
