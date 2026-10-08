"""context.py 的排序/评分/建议函数（此前没有任何测试引用）。"""
from __future__ import annotations

import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from momentum_agent.context import (
    _estimate_energy,
    build_user_context,
    choose_next_action,
    daily_review,
    heartbeat_suggestion,
    priority_rank,
    ranked_tasks,
    task_score,
)
from momentum_agent.models import Priority, TaskStatus
from momentum_agent.storage import SQLiteTaskStore


@pytest.fixture
def store():
    return SQLiteTaskStore(pathlib.Path(tempfile.mkdtemp()) / "context.sqlite3")


def make_context(tasks, **overrides):
    return build_user_context(tasks, **overrides)


def test_priority_rank_orders_high_before_low():
    assert priority_rank("high") < priority_rank("medium") < priority_rank("low")


def test_task_score_rewards_priority_and_overdue(store):
    now = datetime.now(timezone.utc)
    overdue = store.create_task("逾期的高优先", priority=Priority.HIGH, due_at=now - timedelta(days=2))
    future = store.create_task("以后再说", priority=Priority.LOW, due_at=now + timedelta(days=10))
    tasks = [overdue, future]
    context = make_context(tasks)

    assert task_score(overdue, context) > task_score(future, context)


def test_ranked_tasks_sorts_by_score_and_keeps_every_task(store):
    now = datetime.now(timezone.utc)
    tasks = [
        store.create_task("低", priority=Priority.LOW, due_at=now + timedelta(days=5)),
        store.create_task("高", priority=Priority.HIGH, due_at=now - timedelta(hours=1)),
        store.create_task("中", priority=Priority.MEDIUM, due_at=now + timedelta(days=1)),
    ]
    context = make_context(tasks)
    ranked = ranked_tasks(tasks, context)

    assert len(ranked) == len(tasks)
    scores = [task_score(task, context) for task in ranked]
    assert scores == sorted(scores, reverse=True), scores
    assert ranked[0].title == "高", [task.title for task in ranked]


def test_choose_next_action_and_review_are_actionable(store):
    now = datetime.now(timezone.utc)
    tasks = [store.create_task("今天要做的事", due_at=now + timedelta(hours=2))]
    context = make_context(tasks)

    action = choose_next_action(tasks, context)
    assert isinstance(action, str) and action.strip()
    review = daily_review(tasks, context)
    assert isinstance(review, str) and review.strip()


def test_heartbeat_suggestion_handles_empty_and_busy_days(store):
    assert heartbeat_suggestion([], make_context([])) .strip()

    now = datetime.now(timezone.utc)
    tasks = [
        store.create_task(f"任务{index}", due_at=now - timedelta(days=1)) if index % 3 == 0
        else store.create_task(f"任务{index}")
        for index in range(6)
    ]
    suggestion = heartbeat_suggestion(tasks, make_context(tasks))
    assert isinstance(suggestion, str) and suggestion.strip()


def test_estimate_energy_respects_working_hours():
    now = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
    inside = _estimate_energy(now, "09:00", "18:00")
    outside = _estimate_energy(now.replace(hour=3), "09:00", "18:00")
    assert inside and outside
    assert inside != outside, (inside, outside)


def test_build_user_context_exposes_expected_fields(store):
    tasks = [store.create_task("上下文任务")]
    context = make_context(tasks)
    assert context.now is not None
    assert isinstance(context.energy, str) and context.energy
    assert isinstance(context.available_minutes_today, int)

